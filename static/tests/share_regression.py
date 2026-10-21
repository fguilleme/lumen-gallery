"""Browser regression for native file-share preparation and invocation.

Run against the passwordless deployment with:
    uv run --no-project --with playwright python static/tests/share_regression.py

The browser mock replaces navigator.canShare/share only to inspect the app's
invocation contract. It never claims to open a real operating-system share sheet.
All source bytes are fetched from the live original/preview endpoints.
"""

from __future__ import annotations

import json
import os
from typing import Any

from playwright.sync_api import Page, sync_playwright


BASE_URL = os.environ.get("LUMEN_BASE_URL", "https://arcturus.tail38ed6c.ts.net:9443")


MOCK_SHARE_SCRIPT = r"""
(() => {
  window.__shareCalls = [];
  window.__canShareCalls = [];
  window.__shareCanShare = true;
  window.__shareMode = 'resolve';
  const digest = async (bytes) => {
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, '0')).join('');
  };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (payload) => {
      const file = payload?.files?.[0];
      window.__canShareCalls.push({
        hasFiles: Array.isArray(payload?.files),
        name: file?.name || '',
        type: file?.type || '',
        size: file?.size || 0,
      });
      return window.__shareCanShare === true;
    },
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (payload) => {
      const file = payload?.files?.[0];
      if (!file) {
        return Promise.reject(new TypeError('missing file'));
      }
      return file.arrayBuffer().then(async (bytes) => {
        const call = {
          name: file.name,
          type: file.type,
          size: file.size,
          sha256: await digest(bytes),
          url: location.href,
        };
        window.__shareCalls.push(call);
        if (window.__shareMode === 'abort') {
          throw new DOMException('Share cancelled', 'AbortError');
        }
        if (window.__shareMode === 'not-allowed-once') {
          window.__shareMode = 'resolve';
          throw new DOMException('Activation expired', 'NotAllowedError');
        }
        if (window.__shareMode === 'not-allowed') {
          throw new DOMException('Activation expired', 'NotAllowedError');
        }
        return undefined;
      });
    },
  });
})();
"""


def live_items(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(
        """async () => {
          const response = await fetch('/api/photos?sort=date_desc&offset=0&limit=200');
          if (!response.ok) throw new Error(`photos status ${response.status}`);
          const payload = await response.json();
          return payload.items || [];
        }"""
    )


def wait_for_gallery(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator("#app-view").wait_for(state="visible", timeout=60_000)
    page.locator(".photo-card").first.wait_for(timeout=60_000)
    assert page.locator(".photo-download").count() == 0
    assert page.locator("a[download]").count() == 0
    assert page.locator("#lightbox-download").count() == 0


def open_item(page: Page, item: dict[str, Any]) -> None:
    selector = f'.photo-card[data-photo-id="{item["id"]}"] .photo-card__open'
    card = page.locator(selector)
    if card.count() == 0:
        page.locator("#search-input").fill(str(item["name"]))
        page.wait_for_function(
            "(id) => [...document.querySelectorAll('.photo-card')].some((card) => card.dataset.photoId === id)",
            arg=str(item["id"]),
            timeout=30_000,
        )
        card = page.locator(selector)
    card.first.click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)


def close_lightbox(page: Page) -> None:
    if page.locator("#lightbox").get_attribute("open") is not None:
        page.locator("#lightbox-close").click()
        page.locator("#lightbox").wait_for(state="hidden", timeout=10_000)


def original_digest(page: Page, url: str) -> dict[str, Any]:
    return page.evaluate(
        """async (url) => {
          const response = await fetch(url);
          const bytes = await response.arrayBuffer();
          const hash = await crypto.subtle.digest('SHA-256', bytes);
          return {
            status: response.status,
            size: bytes.byteLength,
            type: (response.headers.get('content-type') || '').split(';', 1)[0].trim().toLowerCase(),
            sha256: [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, '0')).join(''),
          };
        }""",
        url,
    )


def share_calls(page: Page) -> list[dict[str, Any]]:
    return page.evaluate("window.__shareCalls.slice()")


def share_one_click_or_reclick(page: Page, selector: str) -> dict[str, Any]:
    before = len(share_calls(page))
    page.locator(selector).click()
    try:
        page.wait_for_function(
            "(count) => window.__shareCalls.length > count",
            arg=before,
            timeout=3_000,
        )
    except PlaywrightTimeoutError:
        status = page.locator("#lightbox-share-status").inner_text()
        assert "prêt" in status.lower(), status
        assert not page.locator(selector).is_disabled()
        page.locator(selector).click()
        page.wait_for_function(
            "(count) => window.__shareCalls.length > count",
            arg=before,
            timeout=3_000,
        )
    return share_calls(page)[-1]


def assert_original_share(page: Page, item: dict[str, Any]) -> dict[str, Any]:
    expected = original_digest(page, item["download_url"])
    assert expected["status"] == 200, expected
    call = share_one_click_or_reclick(page, "#lightbox-share")
    assert call["name"] == item["name"], call
    assert call["type"] == expected["type"], call
    assert call["size"] == expected["size"], call
    assert call["sha256"] == expected["sha256"], call
    assert call["url"] == page.url, call
    return {"expected": expected, "share": call}


def assert_no_error_status(page: Page) -> None:
    status = page.locator("#lightbox-share-status")
    assert not status.evaluate("(element) => element.classList.contains('is-error')")
    assert "échec" not in status.inner_text().lower()


def test_share_contract(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    items = live_items(page)
    jpeg = next(
        (item for item in items if item.get("media_type") == "photo" and str(item.get("format", "")).upper() in {"JPEG", "JPG"}),
        None,
    )
    mov = next(
        (item for item in items if item.get("media_type") == "video" and str(item.get("format", "")).upper() == "MOV"),
        None,
    )
    raw = next(
        (item for item in items if item.get("media_type") == "photo" and str(item.get("format", "")).upper() in {"CR2", "CR3", "DNG", "NEF", "ARW"}),
        None,
    )
    assert jpeg, "live gallery has no JPEG for original-share regression"
    assert mov, "live gallery has no MOV for original-share regression"

    open_item(page, jpeg)
    icon = page.locator("#lightbox-share")
    assert icon.count() == 1
    assert icon.get_attribute("aria-label") == "Partager"
    assert icon.get_attribute("title") == "Partager"
    assert page.locator("#lightbox-share svg").count() == 1
    icon_box = icon.bounding_box()
    assert icon_box and icon_box["width"] >= 44 and icon_box["height"] >= 44, icon_box
    assert "Télécharger" not in page.locator("body").inner_text()
    close_lightbox(page)

    results: dict[str, Any] = {}
    for item in (jpeg, mov):
        close_lightbox(page)
        page.evaluate("window.__shareCanShare = true; window.__shareMode = 'resolve';")
        url_before = page.url
        open_item(page, item)
        results[str(item["format"])] = assert_original_share(page, item)
        assert page.url == url_before
        assert_no_error_status(page)

    close_lightbox(page)
    page.evaluate("window.__shareCanShare = true; window.__shareMode = 'abort';")
    open_item(page, jpeg)
    before_cancel = len(share_calls(page))
    share_one_click_or_reclick(page, "#lightbox-share")
    assert len(share_calls(page)) == before_cancel + 1
    assert_no_error_status(page)

    close_lightbox(page)
    page.evaluate("window.__shareCanShare = false; window.__shareMode = 'resolve';")
    open_item(page, jpeg)
    url_before_unsupported = page.url
    page.locator("#lightbox-share").click()
    page.wait_for_function(
        "() => document.querySelector('#lightbox-share-status')?.textContent.includes('original')",
        timeout=10_000,
    )
    assert len(share_calls(page)) == before_cancel + 1
    assert page.url == url_before_unsupported
    assert page.locator("a[download]").count() == 0
    unsupported = page.locator("#lightbox-share-status").inner_text()
    assert "original" in unsupported.lower(), unsupported

    raw_result = None
    if raw:
        close_lightbox(page)
        page.evaluate("window.__shareCanShare = false; window.__shareMode = 'resolve';")
        open_item(page, raw)
        page.locator("#lightbox-share").click()
        page.wait_for_function(
            "() => document.querySelector('#lightbox-share-status')?.textContent.includes('original')",
            timeout=10_000,
        )
        fallback = page.locator("#lightbox-share-preview")
        assert fallback.is_visible()
        assert fallback.inner_text() == "Partager le JPEG"
        page.evaluate("window.__shareCanShare = true;")
        expected_preview = original_digest(page, raw["preview_url"])
        raw_call = share_one_click_or_reclick(page, "#lightbox-share-preview")
        assert raw_call["name"].lower().endswith(".jpg"), raw_call
        assert raw_call["type"] == expected_preview["type"] == "image/jpeg", raw_call
        assert raw_call["size"] == expected_preview["size"], raw_call
        assert raw_call["sha256"] == expected_preview["sha256"], raw_call
        raw_result = {"expected": expected_preview, "share": raw_call}

    close_lightbox(page)
    if raw_result is not None:
        results["RAW_fallback"] = raw_result
    return results


def test_slow_ready_reclick_and_abort(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    items = live_items(page)
    first = items[0]
    second = items[1]
    delay_state = {"seconds": 0.0}

    def delay_download(route) -> None:
        if delay_state["seconds"]:
            import time

            time.sleep(delay_state["seconds"])
        route.continue_()

    page.route("**/api/photos/*/download", delay_download)
    page.evaluate("window.__shareCanShare = true; window.__shareMode = 'not-allowed-once';")
    page.locator("#search-input").fill("")
    page.wait_for_function("document.querySelectorAll('.photo-card').length >= 2", timeout=30_000)
    open_item(page, first)
    delay_state["seconds"] = 6.2
    page.locator("#lightbox-share").click()
    page.wait_for_function(
        "() => document.querySelector('#lightbox-share-status')?.textContent.includes('Fichier prêt')",
        timeout=15_000,
    )
    assert not page.locator("#lightbox-share").is_disabled()
    delay_state["seconds"] = 0.0
    page.evaluate("window.__shareMode = 'resolve';")
    before_reclick = len(share_calls(page))
    page.locator("#lightbox-share").click()
    page.wait_for_function("(count) => window.__shareCalls.length > count", arg=before_reclick, timeout=5_000)
    ready_reclick = share_calls(page)[-1]

    close_lightbox(page)
    delay_state["seconds"] = 0.8
    before_abort = len(share_calls(page))
    open_item(page, first)
    page.locator("#lightbox-share").click()
    page.locator("#lightbox-next").click()
    page.wait_for_timeout(1_100)
    assert len(share_calls(page)) == before_abort
    assert page.locator("#lightbox-title").inner_text() == second["name"]

    page.locator("#lightbox-share").click()
    page.locator("#lightbox-close").click()
    page.wait_for_timeout(1_100)
    assert len(share_calls(page)) == before_abort
    return {"ready_reclick": ready_reclick, "aborted_share_calls": 0}


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script(MOCK_SHARE_SCRIPT)
        page = context.new_page()
        result = {
            "share_contract": test_share_contract(page),
            "slow_and_abort": test_slow_ready_reclick_and_abort(page),
        }
        context.close()
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
