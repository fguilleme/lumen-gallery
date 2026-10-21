"""Check actual star glyph fill in gallery and viewer without changing ratings."""
from playwright.sync_api import sync_playwright
import json

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width':390,'height':844})
    page.goto('https://arcturus.tail38ed6c.ts.net:9443')
    page.locator('.photo-card').first.wait_for(timeout=30000)
    def validate(selector):
        stars = page.locator(selector).evaluate_all("els => els.map(e => ({filled:e.classList.contains('is-filled'),glyph:e.querySelector('span')?.textContent,pressed:e.getAttribute('aria-pressed')}))")
        assert stars
        for star in stars:
            assert star['glyph'] == ('★' if star['filled'] else '☆'), star
            assert star['pressed'] == str(star['filled']).lower()
        return len(stars)
    gallery = validate('.photo-card .rating-control__star')
    page.locator('.photo-card__open').first.click()
    page.locator('#lightbox').wait_for(state='visible')
    viewer = validate('#lightbox .rating-control__star')
    assert viewer == 4
    print(json.dumps({'gallery_stars_checked':gallery,'viewer_stars_checked':viewer,'selected':'★','unselected':'☆'},ensure_ascii=False))
    browser.close()
