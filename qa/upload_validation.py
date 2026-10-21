"""Exercise real browser uploads, then remove only this probe's own files."""
from pathlib import Path
import json
import sqlite3
import subprocess
import time
import uuid
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://arcturus.tail38ed6c.ts.net:9443'
PREFIX = 'LUMEN-QA-' + uuid.uuid4().hex
OUT = ROOT / 'qa/results'
OUT.mkdir(parents=True, exist_ok=True)
FILES = [OUT / (PREFIX + '.jpg'), OUT / (PREFIX + '.mov')]


def main():
    subprocess.run([str(ROOT / '.venv/bin/python'), '-c', 'from PIL import Image;import sys;Image.new("RGB",(320,240),(60,90,120)).save(sys.argv[1],"JPEG")', str(FILES[0])], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '0.7', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(FILES[1])], check=True)
    report = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width':390, 'height':844}, is_mobile=True, has_touch=True)
            page = context.new_page()
            page.goto(BASE)
            page.locator('.photo-card').first.wait_for(timeout=30000)
            page.locator('#upload-toggle').click()
            page.locator('#upload-input').set_input_files([str(path) for path in FILES])
            assert page.locator('#upload-submit').is_visible()
            assert page.locator('#upload-submit').is_enabled()
            with page.expect_response(lambda response: '/api/upload' in response.url and response.request.method == 'POST') as pending:
                page.locator('#upload-submit').click()
            response = pending.value
            assert response.status == 200
            payload = response.json()
            assert payload['accepted'] == 2 and not payload['errors'], payload
            for _ in range(120):
                status = context.request.get(BASE + '/api/status').json()
                if not status['running']:
                    break
                page.wait_for_timeout(500)
            assert not status['running'] and not status['errors'], status
            result = context.request.get(BASE + '/api/photos', params={'q':PREFIX, 'limit':20}).json()
            assert result['total'] == 2, result
            assert {item['media_type'] for item in result['items']} == {'photo','video'}
            for item in result['items']:
                assert context.request.get(BASE + item['thumb_url']).status == 200
            report = {'upload_HTTP':response.status,'accepted':payload['accepted'],'indexed':result['total'],'types':sorted(item['media_type'] for item in result['items']),'ui_summary':page.locator('#upload-summary').inner_text()}
            browser.close()
    finally:
        # Exact uniquely named probe rows only. Never remove any user's upload.
        database = sqlite3.connect(ROOT / 'data/gallery.sqlite3')
        paths = database.execute('SELECT stored_path FROM upload_names WHERE original_name IN (?,?)', tuple(path.name for path in FILES)).fetchall()
        for (value,) in paths:
            target = Path(value).resolve()
            target.relative_to((ROOT / 'data/uploads').resolve())
            target.unlink(missing_ok=True)
            database.execute('DELETE FROM photos WHERE path=?',(value,))
            database.execute('DELETE FROM upload_names WHERE stored_path=?',(value,))
        database.commit()
        database.close()
        for path in FILES:
            path.unlink(missing_ok=True)
    (OUT / 'upload-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

if __name__ == '__main__':
    main()
