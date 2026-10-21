"""Real-gallery browser regression for scroll jumps during masonry updates.

Run against the passwordless deployment with:
    uv run --no-project --with playwright python static/tests/scroll_regression.py

The test deliberately uses the deployed gallery API and only delays real thumbnail
requests; it never fabricates photo records or reads a password.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from playwright.sync_api import Page, Route, sync_playwright


BASE_URL = os.environ.get(
    "LUMEN_BASE_URL", "https://arcturus.tail38ed6c.ts.net:9443"
)


def measure(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {
              x: box.x,
              y: box.y,
              width: box.width,
              height: box.height,
              bottom: box.bottom,
            };
          };
          const cards = [...document.querySelectorAll('.photo-card')];
          const records = cards.slice(0, 12).map((card) => ({
            id: card.dataset.photoId,
            rect: rect(card),
            frame: rect(card.querySelector('.photo-card__frame')),
            gridRowEnd: getComputedStyle(card).gridRowEnd,
          }));
          const first = records[0];
          const nextInFirstColumn = records.find((item) => (
            first && item.rect.y > first.rect.y + 1 && Math.abs(item.rect.x - first.rect.x) < 1
          ));
          return {
            scrollY: window.scrollY,
            scrollHeight: document.documentElement.scrollHeight,
            viewport: {width: innerWidth, height: innerHeight},
            cards: cards.length,
            overflow: document.documentElement.scrollWidth > innerWidth,
            loadedImages: [...document.querySelectorAll('.photo-card__image')]
              .filter((image) => image.complete && image.naturalWidth > 0).length,
            records,
            firstColumnGap: nextInFirstColumn
              ? nextInFirstColumn.rect.y - first.rect.bottom
              : null,
            anchor: window.__scrollRegressionAnchor
              ? {
                  id: window.__scrollRegressionAnchor.dataset.photoId,
                  rect: rect(window.__scrollRegressionAnchor),
                }
              : null,
            existingCardStable: window.__scrollRegressionExistingCard
              ? window.__scrollRegressionExistingCard === cards[0]
              : null,
          };
        }"""
    )


def wait_for_gallery(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator("#app-view").wait_for(state="visible", timeout=60_000)
    page.locator(".photo-card").first.wait_for(timeout=60_000)
    assert page.locator("#server-details").count() == 0
    assert "Configuration du serveur" not in page.locator("body").inner_text()
    page.wait_for_function(
        "document.querySelectorAll('.photo-card').length >= 24", timeout=60_000
    )
    total = page.evaluate(
        """async () => {
          const response = await fetch('/api/photos?sort=date_desc&offset=0&limit=1');
          const payload = await response.json();
          return Number(payload.total);
        }"""
    )
    assert total > 48, f"live gallery must contain more than 48 photos, got {total}"
    page.evaluate("document.documentElement.style.scrollBehavior = 'auto'")


def capture_anchor(page: Page) -> None:
    page.evaluate(
        """() => {
          const cards = [...document.querySelectorAll('.photo-card')];
          window.__scrollRegressionAnchor = cards.find((card) => {
            const rect = card.getBoundingClientRect();
            return rect.top >= 0 && rect.top < innerHeight * 0.75;
          }) || cards[0] || null;
        }"""
    )


def scroll_to(page: Page, top: float) -> None:
    page.evaluate(
        """(value) => window.scrollTo({top: value, left: 0, behavior: 'instant'})""",
        top,
    )
    page.wait_for_timeout(80)


def touch_swipe_up(page: Page, start_y: int = 700, end_y: int = 250) -> None:
    client = page.context.new_cdp_session(page)
    x = 190
    client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": start_y, "id": 1}]},
    )
    for y in range(start_y - 50, end_y - 1, -50):
        client.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y, "id": 1}]},
        )
        time.sleep(0.01)
    client.send(
        "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}
    )
    page.wait_for_timeout(120)


def touch_swipe_horizontal(page: Page, start_x: int = 300, end_x: int = 90, y: int = 300) -> None:
    client = page.context.new_cdp_session(page)
    client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": start_x, "y": y, "id": 1}]},
    )
    for x in range(start_x - 35, end_x - 1, -35):
        client.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y, "id": 1}]},
        )
        time.sleep(0.01)
    client.send(
        "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}
    )
    page.wait_for_timeout(120)


def inspect_viewer_sizes(page: Page) -> dict[str, Any]:
    sizes = [(390, 650), (390, 844), (320, 568)]
    measurements = []
    for width, height in sizes:
        page.set_viewport_size({"width": width, "height": height})
        page.reload(wait_until="domcontentloaded")
        page.locator(".photo-card").first.wait_for(timeout=60_000)
        page.wait_for_timeout(650)
        page.locator("#filter-details").evaluate("(element) => { element.open = true; }")
        gallery_overflow = page.evaluate(
            """() => {
              const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0
                  && style.display !== 'none' && style.visibility !== 'hidden'
                  && !element.closest('[hidden]');
              };
              const offenders = [...document.querySelectorAll('*')]
                .filter(visible)
                .map((element) => {
                  const rect = element.getBoundingClientRect();
                  return {
                    tag: element.tagName,
                    id: element.id,
                    className: String(element.className || '').slice(0, 100),
                    left: rect.left,
                    right: rect.right,
                  };
                })
                .filter((item) => item.left < -0.5 || item.right > innerWidth + 0.5);
              return {
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                offenders,
              };
            }"""
        )
        page.locator("#filter-details").evaluate("(element) => { element.open = false; }")
        page.evaluate("window.scrollTo({top: 600, left: 0, behavior: 'instant'})")
        page.wait_for_timeout(100)
        before_modal = measure(page)
        page.evaluate(
            """() => {
              const button = [...document.querySelectorAll('.photo-card__open')].find((element) => {
                const rect = element.getBoundingClientRect();
                return rect.top >= 0 && rect.bottom <= innerHeight;
              });
              button?.click();
            }"""
        )
        page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
        page.wait_for_timeout(300)
        lightbox = page.evaluate(
            """() => {
              const rect = (element) => {
                const box = element.getBoundingClientRect();
                return {left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height};
              };
              const dialog = document.querySelector('#lightbox');
              const frame = document.querySelector('#lightbox-image-frame');
              const image = document.querySelector('#lightbox-image');
              const arrows = [document.querySelector('#lightbox-previous'), document.querySelector('#lightbox-next')];
              const header = document.querySelector('.lightbox-header');
              const close = document.querySelector('#lightbox-close');
              return {
                dialog: rect(dialog),
                header: rect(header),
                close: rect(close),
                closeText: close.textContent,
                frame: rect(frame),
                media: {
                  ...rect(document.querySelector('.lightbox-media')),
                  clientWidth: document.querySelector('.lightbox-media').clientWidth,
                  scrollWidth: document.querySelector('.lightbox-media').scrollWidth,
                  clientHeight: document.querySelector('.lightbox-media').clientHeight,
                  scrollHeight: document.querySelector('.lightbox-media').scrollHeight,
                  contentWidth: document.querySelector('.lightbox-media').clientWidth
                    - parseFloat(getComputedStyle(document.querySelector('.lightbox-media')).paddingLeft)
                    - parseFloat(getComputedStyle(document.querySelector('.lightbox-media')).paddingRight),
                  contentHeight: document.querySelector('.lightbox-media').clientHeight
                    - parseFloat(getComputedStyle(document.querySelector('.lightbox-media')).paddingTop)
                    - parseFloat(getComputedStyle(document.querySelector('.lightbox-media')).paddingBottom),
                },
                image: {...rect(image), naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight},
                arrows: arrows.map(rect),
                viewportMeta: document.querySelector('meta[name="viewport"]')?.content || '',
                touchAction: getComputedStyle(document.querySelector('.photo-card__open')).touchAction,
                frameTouchAction: getComputedStyle(frame).touchAction,
                controlFontSizes: [...document.querySelectorAll('input:not([type="hidden"]), select, textarea')]
                  .filter((element) => element.offsetParent !== null
                    && !element.closest('details:not([open])')
                    && !element.closest('[hidden]'))
                  .map((element) => ({id: element.id, fontSize: parseFloat(getComputedStyle(element).fontSize)})),
              };
            }"""
        )
        position_before_swipe = page.locator("#lightbox-position").text_content()
        frame_box = page.locator("#lightbox-image-frame").bounding_box()
        assert frame_box is not None
        touch_swipe_horizontal(
            page,
            start_x=int(frame_box["x"] + frame_box["width"] * 0.72),
            end_x=int(frame_box["x"] + frame_box["width"] * 0.28),
            y=int(frame_box["y"] + frame_box["height"] * 0.5),
        )
        position_after_swipe = page.locator("#lightbox-position").text_content()
        page.locator("#lightbox-close").click()
        page.wait_for_timeout(180)
        after_modal = measure(page)
        result = {
            "viewport": {"width": width, "height": height},
            "galleryOverflow": gallery_overflow,
            "before_modal": before_modal,
            "after_modal": after_modal,
            "lightbox": lightbox,
            "position_before_swipe": position_before_swipe,
            "position_after_swipe": position_after_swipe,
        }
        assert gallery_overflow["scrollWidth"] == gallery_overflow["clientWidth"], result
        assert not gallery_overflow["offenders"], result
        assert lightbox["header"]["top"] >= 0 and lightbox["header"]["bottom"] <= height, result
        assert "Retour" in lightbox["closeText"], result
        assert lightbox["close"]["left"] >= 0 and lightbox["close"]["right"] <= width, result
        assert lightbox["frame"]["left"] >= 0 and lightbox["frame"]["right"] <= width, result
        assert all(
            arrow["width"] >= 44 and arrow["height"] >= 44
            and lightbox["frame"]["left"] <= arrow["left"]
            and arrow["right"] <= lightbox["frame"]["right"]
            and arrow["top"] < lightbox["frame"]["bottom"]
            and arrow["bottom"] > lightbox["frame"]["top"]
            for arrow in lightbox["arrows"]
        ), result
        assert lightbox["image"]["naturalWidth"] > 0, result
        assert abs(
            lightbox["image"]["width"] / lightbox["image"]["height"]
            - lightbox["image"]["naturalWidth"] / lightbox["image"]["naturalHeight"]
        ) < 0.02, result
        assert lightbox["media"]["scrollWidth"] == lightbox["media"]["clientWidth"], result
        assert lightbox["media"]["scrollHeight"] == lightbox["media"]["clientHeight"], result
        assert lightbox["image"]["left"] >= lightbox["media"]["left"] - 1, result
        assert lightbox["image"]["right"] <= lightbox["media"]["right"] + 1, result
        assert lightbox["image"]["top"] >= lightbox["media"]["top"] - 1, result
        assert lightbox["image"]["bottom"] <= lightbox["media"]["bottom"] + 1, result
        image_center = (lightbox["image"]["top"] + lightbox["image"]["bottom"]) / 2
        media_center = (lightbox["media"]["top"] + lightbox["media"]["bottom"]) / 2
        assert abs(image_center - media_center) <= 4, result
        source_ratio = lightbox["image"]["naturalWidth"] / lightbox["image"]["naturalHeight"]
        available_ratio = lightbox["media"]["contentWidth"] / lightbox["media"]["contentHeight"]
        if source_ratio >= available_ratio:
            assert lightbox["image"]["width"] >= lightbox["media"]["contentWidth"] - 4, result
        else:
            assert lightbox["image"]["height"] >= lightbox["media"]["contentHeight"] - 4, result
        assert lightbox["controlFontSizes"] and all(
            item["fontSize"] >= 16 for item in lightbox["controlFontSizes"]
        ), result
        assert lightbox["touchAction"] == "manipulation", result
        assert lightbox["frameTouchAction"] == "pan-y", result
        assert before_modal["scrollY"] == after_modal["scrollY"], result
        assert position_before_swipe != position_after_swipe, result
        measurements.append(result)

    portrait_name = page.evaluate(
        """async () => {
          const response = await fetch('/api/photos?sort=date_desc&offset=0&limit=200');
          const payload = await response.json();
          return payload.items.find((item) => Number(item.height) > Number(item.width))?.name || null;
        }"""
    )
    assert portrait_name, "live gallery has no portrait photo for viewer regression"
    page.locator("#search-input").fill(portrait_name)
    page.wait_for_function(
        "document.querySelectorAll('.photo-card').length === 1", timeout=30_000
    )
    page.locator(".photo-card__open").first.click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.wait_for_function("document.querySelector('#lightbox-image').naturalWidth > 0", timeout=30_000)
    portrait = page.evaluate(
        """() => {
          const frame = document.querySelector('#lightbox-image-frame').getBoundingClientRect();
          const media = document.querySelector('.lightbox-media');
          const image = document.querySelector('#lightbox-image');
          const rect = image.getBoundingClientRect();
          return {
            frame: {width: frame.width, height: frame.height},
            media: {
              ...(() => {
                const box = media.getBoundingClientRect();
                return {left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height};
              })(),
              clientWidth: media.clientWidth,
              scrollWidth: media.scrollWidth,
              clientHeight: media.clientHeight,
              scrollHeight: media.scrollHeight,
              contentWidth: media.clientWidth
                - parseFloat(getComputedStyle(media).paddingLeft)
                - parseFloat(getComputedStyle(media).paddingRight),
              contentHeight: media.clientHeight
                - parseFloat(getComputedStyle(media).paddingTop)
                - parseFloat(getComputedStyle(media).paddingBottom),
            },
            image: {
              left: rect.left,
              top: rect.top,
              right: rect.right,
              bottom: rect.bottom,
              width: rect.width,
              height: rect.height,
              naturalWidth: image.naturalWidth,
              naturalHeight: image.naturalHeight,
            },
          };
        }"""
    )
    page.locator("#lightbox-close").click()
    assert portrait["image"]["naturalHeight"] > portrait["image"]["naturalWidth"], portrait
    assert abs(
        portrait["image"]["width"] / portrait["image"]["height"]
        - portrait["image"]["naturalWidth"] / portrait["image"]["naturalHeight"]
    ) < 0.02, portrait
    assert portrait["media"]["scrollWidth"] == portrait["media"]["clientWidth"], portrait
    assert portrait["media"]["scrollHeight"] == portrait["media"]["clientHeight"], portrait
    assert portrait["image"]["left"] >= portrait["media"]["left"] - 1, portrait
    assert portrait["image"]["right"] <= portrait["media"]["right"] + 1, portrait
    assert portrait["image"]["top"] >= portrait["media"]["top"] - 1, portrait
    assert portrait["image"]["bottom"] <= portrait["media"]["bottom"] + 1, portrait
    image_center_x = (portrait["image"]["left"] + portrait["image"]["right"]) / 2
    media_center_x = (portrait["media"]["left"] + portrait["media"]["right"]) / 2
    image_center_y = (portrait["image"]["top"] + portrait["image"]["bottom"]) / 2
    media_center_y = (portrait["media"]["top"] + portrait["media"]["bottom"]) / 2
    assert abs(image_center_x - media_center_x) <= 4, portrait
    assert abs(image_center_y - media_center_y) <= 4, portrait
    source_ratio = portrait["image"]["naturalWidth"] / portrait["image"]["naturalHeight"]
    available_ratio = portrait["media"]["contentWidth"] / portrait["media"]["contentHeight"]
    if source_ratio >= available_ratio:
        assert portrait["image"]["width"] >= portrait["media"]["contentWidth"] - 4, portrait
    else:
        assert portrait["image"]["height"] >= portrait["media"]["contentHeight"] - 4, portrait
    return {"sizes": measurements, "portrait": portrait}


def inspect_mobile_focus_zoom(page: Page) -> dict[str, Any]:
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.locator(".photo-card").first.wait_for(timeout=60_000)
    page.wait_for_timeout(500)
    page.locator("#filter-details").evaluate("(element) => { element.open = true; }")
    page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
    page.wait_for_timeout(80)
    client = page.context.new_cdp_session(page)
    client.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1.25})
    scaled_before = page.evaluate(
        """() => ({
          scale: visualViewport?.scale || 1,
          offsetLeft: visualViewport?.offsetLeft || 0,
          scrollY: window.scrollY,
          scrollWidth: document.documentElement.scrollWidth,
        })"""
    )
    page.locator("#search-input").focus()
    page.wait_for_timeout(80)
    scaled_after = page.evaluate(
        """() => ({
          scale: visualViewport?.scale || 1,
          offsetLeft: visualViewport?.offsetLeft || 0,
          scrollY: window.scrollY,
          scrollWidth: document.documentElement.scrollWidth,
        })"""
    )
    client.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1})
    initial = page.evaluate(
        """() => ({
          scale: visualViewport?.scale || 1,
          offsetLeft: visualViewport?.offsetLeft || 0,
          scrollY: window.scrollY,
          scrollWidth: document.documentElement.scrollWidth,
        })"""
    )
    focused = []
    for selector in ("#search-input", "#camera-filter", "#date-from", "#iso-min"):
        page.locator(selector).focus()
        page.wait_for_timeout(80)
        focused.append(
            page.evaluate(
                """(id) => ({
                  id,
                  scale: visualViewport?.scale || 1,
                  offsetLeft: visualViewport?.offsetLeft || 0,
                  scrollY: window.scrollY,
                  scrollWidth: document.documentElement.scrollWidth,
                  fontSize: parseFloat(getComputedStyle(document.querySelector(id)).fontSize),
                })""",
                selector,
            )
        )
    page.locator("#filter-details").evaluate("(element) => { element.open = false; }")
    result = {
        "page_scale": {"before_focus": scaled_before, "after_focus": scaled_after},
        "initial": initial,
        "focused": focused,
    }
    assert abs(scaled_before["scale"] - 1.25) <= 0.01, result
    assert abs(scaled_after["scale"] - scaled_before["scale"]) <= 0.01, result
    assert abs(scaled_after["offsetLeft"] - scaled_before["offsetLeft"]) <= 16, result
    assert scaled_before["scrollWidth"] == scaled_after["scrollWidth"] == 390, result
    assert scaled_after["scrollY"] == scaled_before["scrollY"], result
    assert initial["scale"] == 1 and initial["offsetLeft"] == 0, result
    assert initial["scrollWidth"] == 390, result
    assert all(
        item["scale"] == 1
        and item["offsetLeft"] == 0
        and item["scrollWidth"] == 390
        and item["fontSize"] >= 16
        for item in focused
    ), result
    return result


def inspect_compact_lightbox_metadata(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    page.locator(".photo-card__open").first.click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "document.querySelectorAll('#lightbox .exif-row').length > 0", timeout=30_000
    )
    rows = page.evaluate(
        """() => [...document.querySelectorAll('#lightbox .exif-row')].map((row) => ({
          label: row.querySelector('dt')?.textContent || '',
          value: row.querySelector('dd')?.textContent || '',
        }))"""
    )
    page.locator("#lightbox-close").click()
    result = {"rows": rows}
    raw_text = " ".join(f"{row['label']} {row['value']}" for row in rows)
    assert 1 <= len(rows) <= 12, result
    assert not any(token.lower() in raw_text.lower() for token in (
        "serial", "gps", "filepermissions", "previewimagestart", "afarea",
    )), result
    return result


def test_delayed_lazy_load_and_mobile_resize(page: Page, delay_state: dict[str, Any]) -> dict[str, Any]:
    wait_for_gallery(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(100)

    # Scroll while real thumbnail responses are still being released by the route.
    scroll_to(page, 1100)
    capture_anchor(page)
    before = measure(page)
    delay_state["armed"] = True
    page.evaluate(
        """() => {
          const image = [...document.querySelectorAll('.photo-card__image')].find((element) => {
            const rect = element.getBoundingClientRect();
            return rect.top >= 0 && rect.bottom <= innerHeight;
          });
          if (image) {
            const source = image.src.split('?')[0];
            window.setTimeout(() => {
              image.loading = 'eager';
              image.src = `${source}?scroll_regression=${Date.now()}`;
            }, 50);
          }
        }"""
    )
    page.wait_for_timeout(2_000)
    after = measure(page)

    touch_before = measure(page)
    touch_swipe_up(page)
    touch_after = measure(page)

    # A height-only viewport change must not re-run masonry at the same width.
    delay_state["enabled"] = False
    page.reload(wait_until="domcontentloaded")
    page.locator(".photo-card").first.wait_for(timeout=60_000)
    page.wait_for_timeout(600)
    page.set_viewport_size({"width": 390, "height": 650})
    page.wait_for_timeout(180)
    scroll_to(page, 1200)
    capture_anchor(page)
    resize_before = measure(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(400)
    resize_after = measure(page)
    narrow = inspect_viewer_sizes(page)
    focus_zoom = inspect_mobile_focus_zoom(page)

    result = {
        "delayed_lazy_load": {
            "before": before,
            "after": after,
            "delayed_thumb_requests": delay_state["count"],
        },
        "touch_scroll": {"before": touch_before, "after": touch_after},
        "height_only_resize": {"before": resize_before, "after": resize_after},
        "narrow_mobile": narrow,
        "focus_zoom": focus_zoom,
    }
    assert delay_state["count"] > 0, "thumbnail delay route did not intercept real requests"
    assert abs(after["scrollY"] - before["scrollY"]) <= 24, result
    assert after["anchor"] and before["anchor"], result
    assert abs(after["anchor"]["rect"]["y"] - before["anchor"]["rect"]["y"]) <= 48, result
    assert touch_after["scrollY"] > touch_before["scrollY"] + 40, result
    assert abs(resize_after["scrollY"] - resize_before["scrollY"]) <= 24, result
    assert resize_after["anchor"] and resize_before["anchor"], result
    assert abs(
        resize_after["anchor"]["rect"]["y"] - resize_before["anchor"]["rect"]["y"]
    ) <= 48, result
    assert not resize_after["overflow"], result
    assert resize_after["firstColumnGap"] is not None, result
    assert 0 <= resize_after["firstColumnGap"] <= 64, result
    return result


def delayed_thumbnail_route(route: Route, delay_state: dict[str, Any]) -> None:
    is_thumbnail = route.request.url.split("?", 1)[0].endswith("/thumb")
    if delay_state["enabled"] and delay_state.get("armed") and is_thumbnail:
        delay_state["count"] += 1
        time.sleep(1.5)
    route.continue_()


def test_load_more_preserves_lower_page(page: Page) -> dict[str, Any]:
    metadata = inspect_compact_lightbox_metadata(page)
    page.set_viewport_size({"width": 1440, "height": 800})
    page.wait_for_timeout(700)
    page.mouse.wheel(0, 900)
    page.wait_for_timeout(120)
    wheel_before = measure(page)
    page.mouse.wheel(0, 700)
    page.wait_for_timeout(120)
    wheel_after = measure(page)

    scroll_to(page, max(0, page.evaluate("document.documentElement.scrollHeight - innerHeight - 40")))
    page.wait_for_timeout(120)
    page.evaluate(
        "window.__scrollRegressionExistingCard = document.querySelector('.photo-card')"
    )
    before = measure(page)
    page.locator("#load-more").click()
    page.wait_for_function(
        "document.querySelectorAll('.photo-card').length >= 48", timeout=60_000
    )
    page.wait_for_timeout(500)
    after = measure(page)
    result = {
        "metadata": metadata,
        "wheel": {"before": wheel_before, "after": wheel_after},
        "load_more": {"before": before, "after": after},
    }
    assert wheel_after["scrollY"] > wheel_before["scrollY"], result
    assert after["cards"] >= 48, result
    assert abs(after["scrollY"] - before["scrollY"]) <= 32, result
    assert after["existingCardStable"] is True, result
    assert not after["overflow"], result
    assert after["firstColumnGap"] is not None, result
    assert 0 <= after["firstColumnGap"] <= 64, result
    return result


def main() -> None:
    results: dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        mobile_context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=3,
        )
        mobile = mobile_context.new_page()
        delay_state: dict[str, Any] = {"enabled": True, "armed": False, "count": 0}
        mobile.route("**/api/photos/*/thumb*", lambda route: delayed_thumbnail_route(route, delay_state))
        results["mobile"] = test_delayed_lazy_load_and_mobile_resize(mobile, delay_state)
        mobile_context.close()

        desktop_context = browser.new_context(viewport={"width": 1440, "height": 800})
        desktop = desktop_context.new_page()
        results["desktop"] = test_load_more_preserves_lower_page(desktop)
        desktop_context.close()
        browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
