"""Read-only live directory UI acceptance, real Chromium and real APIs."""
import base64
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.environ.get('LUMEN_URL', 'https://arcturus.tail38ed6c.ts.net:9443')
RESULTS = Path(__file__).resolve().parents[2] / 'qa/results/directories'
RESULTS.mkdir(parents=True, exist_ok=True)


def unpack(token):
    return json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))


def run():
    reports = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (320, 375, 390, 1440):
            context = browser.new_context(viewport={'width': width, 'height': 900}, is_mobile=width < 500, has_touch=width < 500)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(BASE)
            page.wait_for_selector('#app-view:not([hidden])')
            assert page.locator('#directory-filter').count() == 1, 'Dossier control missing'
            page.wait_for_function("document.querySelector('#directory-filter').options.length > 1")
            facets = context.request.get(BASE + '/api/facets').json()['directories']
            chosen = next((entry for entry in facets if '/' in entry['label']), facets[0])
            page.locator('#filter-details').evaluate('(el) => el.open = true')
            with page.expect_response(lambda r: '/api/photos?' in r.url and 'directory=' in r.url) as response:
                page.select_option('#directory-filter', chosen['value'])
            payload = response.value.json()
            assert payload['total'] == chosen['count'], (payload['total'], chosen)
            root, parent = unpack(chosen['value'])
            for item in payload['items']:
                item_root, item_parent = unpack(item['directory'])
                assert item_root == root
                assert parent == '.' or item_parent == parent or item_parent.startswith(parent + '/')
            page.wait_for_function("document.querySelectorAll('.photo-card').length > 0")
            assert page.locator('#filter-count').inner_text() == '1 actif'
            with page.expect_response(lambda r: '/api/photos?' in r.url and 'directory_asc' in r.url) as response:
                page.select_option('#sort-select', 'directory_asc')
            assert response.value.status == 200
            page.wait_for_function("""() => {
              const images = [...document.querySelectorAll('.photo-card__image')].slice(0, 3);
              return images.length > 0 && images.every(image => image.complete && image.naturalWidth > 0);
            }""")
            dimensions = page.evaluate('({width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth})')
            assert dimensions['scroll'] <= dimensions['width'], dimensions
            bounds = page.locator('#directory-filter').bounding_box()
            assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
            page.screenshot(path=str(RESULTS / f'folders-{width}.png'))
            with page.expect_response(lambda r: '/api/photos?' in r.url and 'imported_only=true' in r.url):
                page.check('#imported-only-filter')
            assert page.input_value('#directory-filter') == ''
            assert not errors, errors
            reports.append({'width': width, 'folder_count': len(facets), 'selected_total': payload['total'], 'overflow': dimensions, 'errors': errors})
            context.close()
        browser.close()
    (RESULTS / 'report.json').write_text(json.dumps(reports, indent=2))
    print(json.dumps(reports, indent=2))


if __name__ == '__main__':
    run()
