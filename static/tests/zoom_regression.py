"""Chromium touch regression for the staged photo viewer zoom.

This is staging evidence only: the document, CSS, and JavaScript are routed from
qa/staging/zoom-static while every /api request and real media response stays on
the configured gallery origin. It exercises CDP touch input, not a fake gesture
or a physical iPhone/Safari browser.

Run from the repository root with:
    uv run --no-project --with playwright python \
      qa/staging/zoom-static/tests/zoom_regression.py
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, sync_playwright


ROOT = Path("/home/francois/projects/lumen-gallery")
STAGE = ROOT / "qa/staging/zoom-static"
BASE_URL = os.environ.get("LUMEN_BASE_URL", "https://arcturus.tail38ed6c.ts.net:9443").rstrip("/")


def stage_routes(context) -> None:
    index = (STAGE / "index.html").read_text(encoding="utf-8")
    script = (STAGE / "app.js").read_text(encoding="utf-8")
    style = (STAGE / "style.css").read_text(encoding="utf-8")
    origin = BASE_URL

    def route_stage(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path == "/" and request.method == "GET":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=index)
            return
        if parsed.path == "/static/app.js" and request.method == "GET":
            route.fulfill(status=200, content_type="text/javascript; charset=utf-8", body=script)
            return
        if parsed.path == "/static/style.css" and request.method == "GET":
            route.fulfill(status=200, content_type="text/css; charset=utf-8", body=style)
            return
        route.continue_()

    context.route(re.compile(rf"^{re.escape(origin)}/(?:\?.*)?$"), route_stage)
    context.route(re.compile(rf"^{re.escape(origin)}/static/(?:app\.js|style\.css)(?:\?.*)?$"), route_stage)


def wait_for_gallery(page: Page) -> None:
    page.goto(f"{BASE_URL}/?zoom_stage=1", wait_until="domcontentloaded")
    page.locator("#app-view").wait_for(state="visible", timeout=90_000)
    page.locator(".photo-card").first.wait_for(timeout=90_000)
    page.locator("#media-type-filter").select_option("photo")
    page.locator('.photo-card[data-media-type="photo"]').first.wait_for(timeout=60_000)
    page.wait_for_function(
        """() => [...document.querySelectorAll('.photo-card[data-media-type="photo"] .photo-card__image')]
          .slice(0, 3)
          .some((image) => image.complete && image.naturalWidth > 0)""",
        timeout=90_000,
    )


def open_first_photo(page: Page) -> None:
    page.locator('.photo-card[data-media-type="photo"] .photo-card__open').first.click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        """() => {
          const image = document.querySelector('#lightbox-image');
          return image && !image.hidden && image.complete && image.naturalWidth > 0
            && image.getBoundingClientRect().width > 0;
        }""",
        timeout=60_000,
    )


def viewer_state(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const image = document.querySelector('#lightbox-image');
          const frame = document.querySelector('#lightbox-image-frame');
          const media = document.querySelector('.lightbox-media');
          const reset = document.querySelector('#lightbox-reset-zoom');
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {
              left: box.left, top: box.top, right: box.right, bottom: box.bottom,
              width: box.width, height: box.height,
            };
          };
          const style = getComputedStyle(image);
          const matrix = new DOMMatrixReadOnly(style.transform === 'none' ? undefined : style.transform);
          return {
            zoom: Number(image?.dataset.viewerZoom || 1),
            panX: Number(image?.dataset.viewerPanX || 0),
            panY: Number(image?.dataset.viewerPanY || 0),
            transform: style.transform,
            transformScale: matrix.a || 1,
            image: rect(image),
            frame: rect(frame),
            media: rect(media),
            position: document.querySelector('#lightbox-position')?.textContent || '',
            resetHidden: Boolean(reset?.hidden),
            resetLabel: reset?.getAttribute('aria-label') || '',
            frameTouchAction: getComputedStyle(frame).touchAction,
            mediaTouchAction: getComputedStyle(media).touchAction,
            visualViewportScale: visualViewport?.scale || 1,
            documentWidth: document.documentElement.scrollWidth,
            innerWidth,
            bodyOverflow: getComputedStyle(document.body).overflow,
          };
        }"""
    )


def dispatch_touch_with_client(client, event_type: str, points: list[dict[str, int]]) -> None:
    client.send("Input.dispatchTouchEvent", {"type": event_type, "touchPoints": points})


def image_point(page: Page, x_fraction: float = 0.5, y_fraction: float = 0.5) -> tuple[int, int]:
    box = page.locator("#lightbox-image").bounding_box()
    assert box and box["width"] > 40 and box["height"] > 40, box
    return (
        int(box["x"] + box["width"] * x_fraction),
        int(box["y"] + box["height"] * y_fraction),
    )


def pinch_photo(page: Page, *, shift_midpoint: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    x, y = image_point(page)
    client = page.context.new_cdp_session(page)
    start = [
        {"x": x - 36, "y": y, "id": 11},
        {"x": x + 36, "y": y, "id": 12},
    ]
    middle = [
        {"x": x - 56 + shift_midpoint[0], "y": y + shift_midpoint[1], "id": 11},
        {"x": x + 56 + shift_midpoint[0], "y": y + shift_midpoint[1], "id": 12},
    ]
    end = [
        {"x": x - 82 + shift_midpoint[0], "y": y + shift_midpoint[1], "id": 11},
        {"x": x + 82 + shift_midpoint[0], "y": y + shift_midpoint[1], "id": 12},
    ]
    dispatch_touch_with_client(client, "touchStart", start)
    time.sleep(0.03)
    dispatch_touch_with_client(client, "touchMove", middle)
    time.sleep(0.03)
    during = viewer_state(page)
    position_during = page.locator("#lightbox-position").text_content()
    dispatch_touch_with_client(client, "touchMove", end)
    time.sleep(0.03)
    dispatch_touch_with_client(client, "touchEnd", [])
    page.wait_for_timeout(100)
    after = viewer_state(page)
    assert position_during == after["position"], {"during": position_during, "after": after}
    return {"during": during, "after": after}


def one_finger_swipe(page: Page, start_fraction: float = 0.78, end_fraction: float = 0.22) -> None:
    x1, y = image_point(page, start_fraction, 0.5)
    x2, _ = image_point(page, end_fraction, 0.5)
    client = page.context.new_cdp_session(page)
    dispatch_touch_with_client(client, "touchStart", [{"x": x1, "y": y, "id": 21}])
    for x in range(x1 - 30, x2 - 1, -30):
        dispatch_touch_with_client(client, "touchMove", [{"x": x, "y": y, "id": 21}])
        time.sleep(0.01)
    dispatch_touch_with_client(client, "touchMove", [{"x": x2, "y": y, "id": 21}])
    dispatch_touch_with_client(client, "touchEnd", [])
    page.wait_for_timeout(150)


def single_tap(page: Page, x_fraction: float = 0.5, y_fraction: float = 0.5) -> None:
    x, y = image_point(page, x_fraction, y_fraction)
    client = page.context.new_cdp_session(page)
    dispatch_touch_with_client(client, "touchStart", [{"x": x, "y": y, "id": 41}])
    dispatch_touch_with_client(client, "touchEnd", [])
    page.wait_for_timeout(70)


def double_tap_toggle(page: Page) -> dict[str, Any]:
    single_tap(page, 0.36, 0.42)
    single_tap(page, 0.36, 0.42)
    zoomed = viewer_state(page)
    single_tap(page, 0.36, 0.42)
    single_tap(page, 0.36, 0.42)
    reset = viewer_state(page)
    return {"zoomed": zoomed, "reset": reset}


def pan_to_edge_twice(page: Page) -> dict[str, Any]:
    x, y = image_point(page)
    client = page.context.new_cdp_session(page)
    dispatch_touch_with_client(client, "touchStart", [{"x": x, "y": y, "id": 31}])
    dispatch_touch_with_client(client, "touchMove", [{"x": 2, "y": 2, "id": 31}])
    page.wait_for_timeout(50)
    first = viewer_state(page)
    dispatch_touch_with_client(client, "touchMove", [{"x": 1, "y": 1, "id": 31}])
    page.wait_for_timeout(50)
    second = viewer_state(page)
    dispatch_touch_with_client(client, "touchEnd", [])
    return {"first": first, "second": second}


def assert_no_page_zoom_or_overflow(state: dict[str, Any]) -> None:
    assert abs(state["visualViewportScale"] - 1) < 0.01, state
    assert state["documentWidth"] <= state["innerWidth"] + 1, state


def assert_image_still_covers_frame(state: dict[str, Any]) -> None:
    image = state["image"]
    frame = state["frame"]
    assert image["right"] > frame["left"] + 1 and image["left"] < frame["right"] - 1, state
    assert image["bottom"] > frame["top"] + 1 and image["top"] < frame["bottom"] - 1, state


def rating_snapshot(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const control = document.querySelector('#lightbox-rating .rating-control');
          return {
            id: control?.dataset.photoId || '',
            rating: Number(control?.dataset.rating || 0),
          };
        }"""
    )


def restore_rating(page: Page, snapshot: dict[str, Any]) -> None:
    if not snapshot.get("id"):
        return
    result = page.evaluate(
        """async ({id, rating}) => {
          const response = await fetch(`/api/photos/${encodeURIComponent(id)}/rating`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rating}),
          });
          return {status: response.status, body: await response.text()};
        }""",
        snapshot,
    )
    assert result["status"] == 200, result
    verified = page.evaluate(
        """async (id) => {
          const response = await fetch(`/api/photos/${encodeURIComponent(id)}`);
          const payload = await response.json();
          return {
            status: response.status,
            rating: payload?.rating ?? payload?.item?.rating ?? null,
          };
        }""",
        str(snapshot["id"]),
    )
    assert verified["status"] == 200 and int(verified["rating"] or 0) == int(snapshot["rating"]), verified


def run_regression(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    open_first_photo(page)
    initial = viewer_state(page)
    assert initial["zoom"] == 1 and initial["panX"] == 0 and initial["panY"] == 0, initial
    assert initial["resetHidden"], initial
    assert initial["resetLabel"] == "Réinitialiser le zoom", initial
    assert initial["mediaTouchAction"] == "none", initial
    assert initial["frameTouchAction"] == "pan-y", initial
    assert_no_page_zoom_or_overflow(initial)

    pinch = pinch_photo(page, shift_midpoint=(10, 8))
    zoomed = pinch["after"]
    assert zoomed["zoom"] > 1.2, pinch
    assert zoomed["zoom"] <= 5, pinch
    assert zoomed["transformScale"] > 1.2, pinch
    assert zoomed["image"]["width"] > initial["image"]["width"] * 1.15, pinch
    assert zoomed["resetHidden"] is False, zoomed
    assert_no_page_zoom_or_overflow(zoomed)
    assert_image_still_covers_frame(zoomed)
    assert pinch["during"]["position"] == initial["position"], pinch

    pan = pan_to_edge_twice(page)
    assert abs(pan["first"]["panX"] - pan["second"]["panX"]) <= 1.5, pan
    assert abs(pan["first"]["panY"] - pan["second"]["panY"]) <= 1.5, pan
    assert_image_still_covers_frame(pan["second"])
    assert_no_page_zoom_or_overflow(pan["second"])

    page.locator("#lightbox-reset-zoom").click()
    page.wait_for_function(
        """() => {
          const image = document.querySelector('#lightbox-image');
          return image?.dataset.viewerZoom === '1' && image?.dataset.viewerPanX === '0'
            && document.querySelector('#lightbox-reset-zoom')?.hidden;
        }"""
    )
    reset = viewer_state(page)
    assert reset["zoom"] == 1 and reset["panX"] == 0 and reset["panY"] == 0, reset
    assert abs(reset["image"]["width"] - initial["image"]["width"]) < 2, {"initial": initial, "reset": reset}

    double_tap = double_tap_toggle(page)
    assert double_tap["zoomed"]["zoom"] > 1 and not double_tap["zoomed"]["resetHidden"], double_tap
    assert_no_page_zoom_or_overflow(double_tap["zoomed"])
    assert double_tap["reset"]["zoom"] == 1 and double_tap["reset"]["resetHidden"], double_tap

    position_before_swipe = reset["position"]
    one_finger_swipe(page)
    position_after_swipe = page.locator("#lightbox-position").text_content()
    assert position_after_swipe != position_before_swipe, {
        "before": position_before_swipe,
        "after": position_after_swipe,
    }
    at_one_after_swipe = viewer_state(page)
    assert at_one_after_swipe["zoom"] == 1, at_one_after_swipe

    page.locator("#lightbox-close").click()
    page.locator("#lightbox").wait_for(state="hidden", timeout=10_000)
    open_first_photo(page)
    reset_snapshot = viewer_state(page)
    assert reset_snapshot["zoom"] == 1 and reset_snapshot["panX"] == 0, reset_snapshot

    original_rating = rating_snapshot(page)
    try:
        pinch_photo(page)
        zoomed_for_controls = viewer_state(page)
        assert zoomed_for_controls["zoom"] > 1, zoomed_for_controls
        share = page.locator("#lightbox-share")
        assert share.is_visible() and not share.is_disabled()
        share.click()
        page.wait_for_timeout(100)
        assert page.locator("#lightbox-share-status").is_visible()

        target_rating = 3 if original_rating["rating"] != 3 else 2
        star = page.locator(
            f'#lightbox-rating .rating-control[data-photo-id="{original_rating["id"]}"] '
            f'button[data-rating-value="{target_rating}"]'
        )
        with page.expect_response(
            re.compile(rf"/api/photos/{re.escape(original_rating['id'])}/rating(?:\?|$)"),
            timeout=15_000,
        ):
            star.click()
        page.wait_for_function(
            """(value) => document.querySelector('#lightbox-rating .rating-control')?.dataset.rating === String(value)""",
            arg=target_rating,
            timeout=10_000,
        )
        rating_zoomed = viewer_state(page)
        assert rating_zoomed["zoom"] > 1, rating_zoomed
    finally:
        restore_rating(page, original_rating)

    page.locator("#lightbox-next").click()
    page.wait_for_timeout(120)
    after_next = viewer_state(page)
    assert after_next["zoom"] == 1 and after_next["panX"] == 0 and after_next["panY"] == 0, after_next
    assert after_next["resetHidden"], after_next

    pinch_photo(page)
    page.locator("#lightbox-close").click()
    page.locator("#lightbox").wait_for(state="hidden", timeout=10_000)
    open_first_photo(page)
    after_close_reopen = viewer_state(page)
    assert after_close_reopen["zoom"] == 1 and after_close_reopen["panX"] == 0, after_close_reopen
    assert_no_page_zoom_or_overflow(after_close_reopen)

    pinch_photo(page)
    page.set_viewport_size({"width": 360, "height": 720})
    page.wait_for_timeout(150)
    after_resize = viewer_state(page)
    assert after_resize["zoom"] == 1 and after_resize["panX"] == 0 and after_resize["panY"] == 0, after_resize
    assert_no_page_zoom_or_overflow(after_resize)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(120)

    wheel_origin = image_point(page)
    page.mouse.move(*wheel_origin)
    page.mouse.wheel(0, -120)
    page.wait_for_timeout(100)
    after_wheel = viewer_state(page)
    assert after_wheel["zoom"] > 1 and after_wheel["documentWidth"] <= after_wheel["innerWidth"] + 1, after_wheel
    page.locator("#lightbox-reset-zoom").click()

    video_result = None
    video_count = page.evaluate(
        """async () => {
          const response = await fetch('/api/photos?media_type=video&sort=date_desc&offset=0&limit=1');
          const payload = await response.json();
          return Number(payload?.total || payload?.items?.length || 0);
        }"""
    )
    page.locator("#lightbox-close").click()
    if video_count:
        page.locator("#media-type-filter").select_option("video")
        page.locator('.photo-card[data-media-type="video"] .photo-card__open').first.click()
        page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
        page.locator("#lightbox-video").wait_for(state="visible", timeout=10_000)
        video_result = page.evaluate(
            """() => ({
              controls: document.querySelector('#lightbox-video')?.controls || false,
              imageHidden: document.querySelector('#lightbox-image')?.hidden || false,
              resetHidden: document.querySelector('#lightbox-reset-zoom')?.hidden || false,
              frameTouchAction: getComputedStyle(document.querySelector('#lightbox-image-frame')).touchAction,
              mediaTouchAction: getComputedStyle(document.querySelector('.lightbox-media')).touchAction,
              mediaClass: document.querySelector('.lightbox-media')?.className || '',
            })"""
        )
        assert video_result["controls"] and video_result["imageHidden"] and video_result["resetHidden"], video_result
        assert video_result["mediaTouchAction"] == "auto" and "is-video" in video_result["mediaClass"], video_result
        page.locator("#lightbox-close").click()

    return {
        "stage": str(STAGE),
        "origin": BASE_URL,
        "initial": initial,
        "pinch": pinch,
        "pan": pan,
        "afterSwipe": at_one_after_swipe,
        "afterNext": after_next,
        "afterCloseReopen": after_close_reopen,
        "afterResize": after_resize,
        "afterWheel": after_wheel,
        "video": video_result,
        "positionBeforeSwipe": position_before_swipe,
        "positionAfterSwipe": position_after_swipe,
    }


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=1,
            has_touch=True,
            is_mobile=True,
        )
        stage_routes(context)
        page = context.new_page()
        result = run_regression(page)
        context.close()
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
