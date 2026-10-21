from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT = Path('/home/francois/projects/lumen-gallery')
BASE = 'http://100.90.227.52:8093'


def load_authenticated_page(page, context):
    page.goto(BASE, wait_until='domcontentloaded')
    password = (ROOT / 'data/access-password').read_text().strip()
    response = context.request.post(BASE + '/api/login', data={'password': password})
    assert response.status == 200, f'login failed: {response.status}'
    page.reload(wait_until='domcontentloaded')
    page.locator('#app-view').wait_for(state='visible', timeout=90000)
    page.locator('.photo-card').first.wait_for(timeout=90000)
    page.wait_for_function(
        """() => {
          const images = [...document.querySelectorAll('.photo-card__image')].slice(0, 5);
          return images.length > 0 && images.every((image) => image.complete && image.naturalWidth > 0);
        }""",
        timeout=90000,
    )
    page.evaluate('window.scrollTo(0, 0)')


def measure(page, width, height):
    page.set_viewport_size({'width': width, 'height': height})
    page.reload(wait_until='domcontentloaded')
    page.locator('#app-view').wait_for(state='visible', timeout=90000)
    page.locator('.photo-card').first.wait_for(timeout=90000)
    page.wait_for_function(
        """() => {
          const images = [...document.querySelectorAll('.photo-card__image')].slice(0, 5);
          return images.length > 0 && images.every((image) => image.complete && image.naturalWidth > 0);
        }""",
        timeout=90000,
    )
    page.evaluate('window.scrollTo(0, 0)')
    return page.evaluate("""() => {
      const rect = (element) => {
        const box = element.getBoundingClientRect();
        return {x: box.x, y: box.y, width: box.width, height: box.height, bottom: box.bottom};
      };
      const cards = [...document.querySelectorAll('.photo-card')].map((card) => ({
        card: rect(card),
        frame: rect(card.querySelector('.photo-card__frame')),
        image: card.querySelector('.photo-card__image'),
      }));
      const first = cards[0];
      const nextInFirstColumn = cards.find((item) => (
        item.card.y > first.card.y + 1 && Math.abs(item.card.x - first.card.x) < 1
      ));
      return {
        viewport: {width: innerWidth, height: innerHeight},
        overflow: document.documentElement.scrollWidth > innerWidth,
        firstPhotoY: first.card.y,
        firstPhotoFrameRatio: first.frame.width / first.frame.height,
        firstPhotoSourceRatio: first.image ? first.image.naturalWidth / first.image.naturalHeight : null,
        allFramesReserved: cards.every((item) => item.frame.width > 0 && item.frame.height > 0),
        gapBelowFirstColumnCard: nextInFirstColumn ? nextInFirstColumn.card.y - first.card.bottom : null,
        cards: cards.length,
        firstImagesLoaded: [...document.querySelectorAll('.photo-card__image')].slice(0, 5)
          .every((image) => image.complete && image.naturalWidth > 0),
      };
    }""")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=1)
        page = context.new_page()
        load_authenticated_page(page, context)
        measurements = {
            'mobile': measure(page, 390, 844),
            'desktop': measure(page, 1440, 1000),
        }
        failures = []
        for name, result in measurements.items():
            if result['overflow']:
                failures.append(f'{name}: horizontal overflow')
            if result['firstPhotoY'] >= 700:
                failures.append(f"{name}: first photo starts at y={result['firstPhotoY']:.1f}")
            if result['gapBelowFirstColumnCard'] is None or result['gapBelowFirstColumnCard'] > 64:
                failures.append(
                    f"{name}: gap below first-column card is {result['gapBelowFirstColumnCard']}px"
                )
            if not result['firstImagesLoaded']:
                failures.append(f'{name}: first five thumbnails are not loaded')
            if not result['firstPhotoFrameRatio'] or not result['firstPhotoSourceRatio']:
                failures.append(f'{name}: first thumbnail has no reserved aspect-ratio frame')
            if not result['allFramesReserved']:
                failures.append(f'{name}: one or more thumbnail frames have no reserved size')
        print(json.dumps({'measurements': measurements, 'failures': failures}, ensure_ascii=False))
        browser.close()
    assert not failures, '; '.join(failures)


if __name__ == '__main__':
    main()
