from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://arcturus.tail38ed6c.ts.net:9443'
OUT = ROOT / 'qa' / 'results'
OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE)
    session = context.request.get(BASE + '/api/session').json()
    if session.get('auth_required') is False:
        assert context.request.get(BASE + '/api/photos').status == 200
        assert not page.locator('#login-view').is_visible()
    else:
        page.locator('#login-view').wait_for(state='visible')
        assert context.request.get(BASE + '/api/photos').status == 401
        response = context.request.post(BASE + '/api/login', data={'password': (ROOT / 'data/access-password').read_text().strip()})
        assert response.status == 200
        page.reload()
    page.locator('#app-view').wait_for(state='visible')
    page.locator('.photo-card__open').first.wait_for(timeout=90000)
    page.wait_for_function("Array.from(document.querySelectorAll('.photo-card__image')).some(i=>i.complete&&i.naturalWidth>0)", timeout=90000)
    page.screenshot(path=str(OUT / 'desktop.png'), full_page=True)
    page.locator('.photo-card__open').first.click()
    page.locator('#lightbox').wait_for(state='visible')
    page.wait_for_function("document.querySelector('#lightbox-image').naturalWidth>0", timeout=60000)
    page.screenshot(path=str(OUT / 'lightbox.png'))
    page.keyboard.press('ArrowRight')
    page.keyboard.press('Escape')
    assert not page.locator('#lightbox').is_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(OUT / 'mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
    page.locator('#filter-details').evaluate('(el) => el.open=true')
    page.locator('#format-filter').select_option('CR2')
    page.wait_for_timeout(1500)
    assert page.locator('.photo-card').count() > 0
    assert not errors, errors
    status = context.request.get(BASE + '/api/status').json()
    result = {'page_errors':errors,'mobile_overflow':False,'rendered_cards':page.locator('.photo-card').count(),'status':status,'screenshots':[str(OUT / name) for name in ['desktop.png','lightbox.png','mobile.png']]}
    (OUT/'browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    browser.close()
