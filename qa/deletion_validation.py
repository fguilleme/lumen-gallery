"""Real backend/browser lifecycle QA using only isolated generated media."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
import threading
import time
import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright
from app import create_app
from gallery.config import Settings
from gallery.trash_worker import process_due_jobs, restore_job

ROOT = Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='lumen-trash-qa-', dir=os.environ['TMPDIR']) as temp:
        folder = Path(temp)
        source = folder / 'source'
        source.mkdir()
        for name in ['zero.jpg','keep.jpg']:
            Image.new('RGB',(320,240),(70,100,130)).save(source/name)
        originals = {p.name:p.read_bytes() for p in source.iterdir()}
        settings = Settings(project_dir=ROOT, photo_root=source, data_dir=folder/'data', auth_required=False)
        app = create_app(settings)
        scanner = app.state.lumen_scanner
        scanner.start()
        deadline = time.monotonic()+20
        while scanner.snapshot()['running'] and time.monotonic()<deadline:
            time.sleep(.02)
        assert not scanner.snapshot()['errors']
        database = app.state.lumen_database
        rows,_ = database.list_photos()
        zero = next(r for r in rows if r['name']=='zero.jpg')
        keep = next(r for r in rows if r['name']=='keep.jpg')
        database.update_rating(keep['id'],1)
        config = uvicorn.Config(app, host='127.0.0.1',port=18094,log_level='error')
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run,daemon=True)
        thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started and time.monotonic()<deadline:
                time.sleep(.02)
            assert server.started
            with sync_playwright() as p:
                browser=p.chromium.launch()
                context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                page=context.new_page()
                errors=[]
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:18094')
                page.locator('.photo-card').first.wait_for()
                page.wait_for_function("document.querySelector('#zero-deletion-count').textContent==='1'")
                assert page.locator('#search-input').count()==0
                assert 'Lumière' not in page.locator('body').inner_text()
                page.locator('#schedule-zero-button').click()
                page.locator('#schedule-zero-dialog').wait_for(state='visible')
                assert 'hors des filtres' in page.locator('#schedule-zero-message').inner_text()
                page.locator('#schedule-zero-confirm').click()
                page.wait_for_function("document.querySelectorAll('.photo-card__deletion').length>0",timeout=10000)
                assert database.deletion_status()['scheduled']==1
                page.locator('#filter-details').evaluate('(e)=>e.open=true')
                page.locator('#deletion-status-filter').select_option('scheduled')
                page.wait_for_function("document.querySelectorAll('.photo-card').length===1")
                page.locator('#filter-details').evaluate('(e)=>e.open=false')
                card=page.locator('.photo-card').first
                card.locator('.rating-control button[data-rating-value="1"]').click()
                deadline=time.monotonic()+10
                while database.deletion_status()['scheduled'] and time.monotonic()<deadline:
                    page.wait_for_timeout(50)
                assert database.deletion_status()['scheduled']==0
                assert database.get_photo(zero['id'])['rating']==1
                assert not errors,errors
                browser.close()
            # The real maintenance functions operate only on the isolated fixture.
            database.update_rating(zero['id'],0)
            database.schedule_zero(now=datetime(2024,1,31,tzinfo=timezone.utc))
            dry=process_due_jobs(settings,database,now=datetime(2024,3,1,tzinfo=timezone.utc))
            assert dry['moved']==0 and (source/'zero.jpg').exists()
            moved=process_due_jobs(settings,database,now=datetime(2024,3,1,tzinfo=timezone.utc),apply=True)
            assert moved['moved']==1 and not moved['errors'],moved
            job=database.get_deletion_job_for_media(zero['id'])
            assert Path(job['trash_path']).read_bytes()==originals['zero.jpg']
            result=restore_job(settings,database,job['id'])
            assert result['restored'],result
            assert (source/'zero.jpg').read_bytes()==originals['zero.jpg']
            assert database.get_photo(zero['id'])['rating']==1
            report={'isolated_browser_schedule':True,'rating_cancels':True,'calendar_month_due':job['due_at'],'dry_run_moves':dry['moved'],'trash_moves':moved['moved'],'recovered_original_bytes':True,'page_errors':errors,'production_files_touched':0}
            (ROOT/'qa/results/deletion-real-validation.json').write_text(json.dumps(report,indent=2))
            print(json.dumps(report))
        finally:
            server.should_exit=True
            thread.join(timeout=10)

if __name__=='__main__':
    main()
