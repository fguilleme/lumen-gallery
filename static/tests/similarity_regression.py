"""Read-only acceptance against live similarity API and real Chromium."""
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

BASE = 'https://arcturus.tail38ed6c.ts.net:9443'
RESULTS = Path(__file__).resolve().parents[2] / 'qa/results/similarity'


def get(path, **params):
    with urlopen(BASE+path+('?' + urlencode(params) if params else ''),timeout=60) as response:
        return json.load(response)


def all_items(**params):
    items = []
    while True:
        result = get('/api/photos',offset=len(items),limit=200,**params)
        if not result['items']:
            assert len(items) == result['total']
            return items
        items.extend(result['items'])
        if len(items) >= result['total']:
            assert len(items) == result['total']
            return items


def main():
    from playwright.sync_api import sync_playwright
    RESULTS.mkdir(parents=True,exist_ok=True)
    status = get('/api/similarity/status')
    assert not status['running'] and status['analyzed'] > 0, status
    reference = all_items()
    ids = {item['id'] for item in reference}
    report = {'status':status,'modes':{},'browsers':[]}
    for mode in ('near','content'):
        items = all_items(similarity=mode)
        assert len(items) == len(ids) == len({item['id'] for item in items})
        assert {item['id'] for item in items} == ids
        groups = Counter(item['similarity_group'] for item in items if item['similarity_group'])
        assert groups, f'{mode}: no real groups found'
        seen = set()
        previous = None
        for item in items:
            group = item['similarity_group']
            if group:
                assert item['media_type'] == 'photo'
                assert item['similarity_size'] == groups[group] >= 2
                if group != previous:
                    assert group not in seen
                    seen.add(group)
            previous = group
        zero = all_items(similarity=mode,rating=0)
        assert all(item['rating'] == 0 for item in zero)
        assert len(zero) == sum(item['rating']==0 for item in reference)
        report['modes'][mode] = {'total':len(items),'groups':len(groups),'grouped':sum(groups.values())}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (320,375,390,1440):
            context = browser.new_context(viewport={'width':width,'height':900},is_mobile=width<500,has_touch=width<500)
            page = context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(BASE)
            page.wait_for_selector('#app-view:not([hidden])')
            for mode in ('near','content'):
                with page.expect_response(lambda r: '/api/photos?' in r.url and f'similarity={mode}' in r.url) as response:
                    page.select_option('#similarity-select',mode)
                payload=response.value.json()
                page.wait_for_function("document.querySelectorAll('.similarity-group-heading').length > 0")
                page.wait_for_function("""() => {
                    const imgs=[...document.querySelectorAll('.photo-card__image')].slice(0,3);
                    return imgs.length>0 && imgs.every(img=>img.complete && img.naturalWidth>0);
                }""")
                page.wait_for_function("""() => [...document.querySelectorAll('.photo-card')].slice(0,3)
                    .every(card => Number(getComputedStyle(card).opacity) > 0.99)""")
                text_fit=page.locator('#similarity-select').evaluate("""el => {
                    const style=getComputedStyle(el), canvas=document.createElement('canvas');
                    const context=canvas.getContext('2d'); context.font=style.font;
                    return {text:context.measureText(el.selectedOptions[0].text).width,
                        available:el.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)};
                }""")
                assert text_fit['text'] <= text_fit['available'] + 1,text_fit
                page.evaluate('window.scrollTo(0,0)')
                overflow=page.evaluate('({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})')
                assert overflow['scroll']<=overflow['width'],overflow
                assert payload['total']==len(ids)
                heading=page.locator('.similarity-group-heading').first
                assert heading.is_visible()
                assert page.locator('#gallery-grid').evaluate("el=>getComputedStyle(el).gridAutoFlow")=='row'
                page.screenshot(path=str(RESULTS/f'{mode}-{width}.png'))
                before=page.locator('.photo-card').count()
                if page.locator('#load-more').is_visible():
                    first=page.locator('.photo-card').first.element_handle()
                    page.locator('#load-more').click()
                    page.wait_for_function('(n)=>document.querySelectorAll(".photo-card").length>n',arg=before)
                    assert first.evaluate('el=>el.isConnected')
                    headings=page.locator('.similarity-group-heading').evaluate_all("els=>els.map(el=>el.dataset.similarityGroup)")
                    actual=[group for group in headings if group]
                    assert len(actual)==len(set(actual)),actual
                report['browsers'].append({'width':width,'mode':mode,'overflow':overflow,'errors':errors.copy()})
            assert not errors,errors
            context.close()
        browser.close()
    (RESULTS/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
