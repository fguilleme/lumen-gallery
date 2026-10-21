"""Validate deployed video support against readonly source inventory."""
from pathlib import Path
from io import BytesIO
from collections import Counter
import hashlib
import json
import subprocess
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://arcturus.tail38ed6c.ts.net:9443'
RESULTS = ROOT / 'qa/results'

def main():
    inventory = json.loads((RESULTS / 'video-source-inventory.json').read_text())
    with httpx.Client(base_url=BASE, timeout=180) as client:
        collected = []
        offset = 0
        while True:
            response = client.get('/api/photos', params={'media_type': 'video', 'offset': offset, 'limit': 100})
            response.raise_for_status()
            payload = response.json()
            collected.extend(payload['items'])
            offset += len(payload['items'])
            if offset >= payload['total']:
                break
            assert payload['items'], 'pagination stopped prematurely'
        indexed = {item['id']: item for item in collected}
        assert len(indexed) == payload['total'] == len(inventory), (len(indexed), payload['total'], len(inventory))
        previews = []
        samples = {}
        for item in indexed.values():
            assert item['media_type'] == 'video'
            assert item['duration'] > 0
            response = client.get(item['thumb_url'])
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as image:
                size = image.size
                image.verify()
            assert abs(size[0] / size[1] - item['width'] / item['height']) < .02, item
            previews.append({'id': item['id'], 'size': size, 'duration': item['duration']})
            samples.setdefault(item['format'], item)
        streams = []
        for fmt, item in samples.items():
            response = client.get(item['stream_url'], headers={'Range': 'bytes=0-1023'})
            assert response.status_code == 206, (fmt, response.status_code, response.text[:100])
            assert len(response.content) == 1024
            assert response.headers['content-type'].startswith('video/')
            response = client.get(item['stream_url'])
            response.raise_for_status()
            output = RESULTS / f'video-stream-sample-{fmt.lower()}.mp4'
            output.write_bytes(response.content)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)]))
            video = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
            audio = next((stream for stream in probe['streams'] if stream['codec_type'] == 'audio'), {})
            assert video['codec_name'] == 'h264'
            assert audio.get('codec_name') in ('aac', None), audio
            assert abs(float(probe['format']['duration']) - item['duration']) < 1
            streams.append({'format': fmt, 'id': item['id'], 'range': 206, 'video': video['codec_name'], 'audio': audio.get('codec_name'), 'duration': probe['format']['duration']})
        for entry in inventory:
            source = Path(entry['path'])
            stat = source.stat()
            assert (stat.st_size, stat.st_mtime_ns) == (entry['size'], entry['mtime_ns']), source
            digest = hashlib.sha256()
            with source.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            assert digest.hexdigest() == entry['sha256'], source
        total = client.get('/api/photos', params={'limit': 1}).json()['total']
        report = {'videos': len(indexed), 'formats': dict(Counter(item['format'] for item in indexed.values())), 'total_media': total, 'thumbnails_verified': len(previews), 'streams': streams, 'originals_sha256_unchanged': len(inventory)}
        (RESULTS / 'video-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))

if __name__ == '__main__':
    main()
