"""Real-gallery browser regression for live video cards and lightbox playback.

Run against the passwordless deployment after video indexing with:
    uv run --no-project --with playwright python static/tests/video_regression.py

The script reads the deployed API and UI only. It never fabricates media records,
intercepts media requests, or reads a password.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from playwright.sync_api import Page, sync_playwright


BASE_URL = os.environ.get(
    "LUMEN_BASE_URL", "https://arcturus.tail38ed6c.ts.net:9443"
)


def live_video_payload(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """async () => {
          const response = await fetch('/api/photos?media_type=video&sort=date_desc&offset=0&limit=200');
          return {status: response.status, payload: await response.json()};
        }"""
    )


def wait_for_gallery(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator("#app-view").wait_for(state="visible", timeout=60_000)
    page.locator(".photo-card").first.wait_for(timeout=60_000)
    assert page.locator("#server-details").count() == 0
    assert "Configuration du serveur" not in page.locator("body").inner_text()


def assert_video_contract(items: list[dict[str, Any]]) -> None:
    assert items, "live gallery has no indexed videos yet"
    if any("media_type" not in item for item in items):
        raise AssertionError("live API response lacks the additive media_type contract")
    for item in items:
        assert item.get("media_type") == "video", item
        assert item.get("stream_url", "").endswith(f"/{item['id']}/stream"), item
        assert item.get("thumb_url"), item
        assert item.get("preview_url"), item
        assert item.get("download_url"), item
        assert item.get("format", "").upper() in {"MOV", "MP4"}, item
        assert int(item.get("width", 0)) > 0 and int(item.get("height", 0)) > 0, item
        assert item.get("duration") is None or float(item["duration"]) >= 0, item


def inspect_video_lightbox(page: Page, video_id: str, preview_url: str, has_next: bool) -> dict[str, Any]:
    assert page.locator("#media-type-filter").input_value() == ""
    assert page.locator("#media-type-filter option").all_text_contents() == [
        "Tous", "Photos", "Vidéos"
    ]
    page.locator("#upload-toggle").click()
    upload_submit = page.locator("#upload-submit")
    assert upload_submit.is_visible() and upload_submit.is_disabled()
    page.locator("#upload-toggle").click()
    format_options = set(page.locator("#format-filter option").all_text_contents())
    assert {"MOV", "MP4"}.issubset(format_options), format_options
    with page.expect_request(
        re.compile(r"/api/photos\?[^#]*media_type=video(?:&|$)"), timeout=30_000
    ):
        page.locator("#media-type-filter").select_option("video")
    page.wait_for_function(
        """(expectedId) => {
          const cards = [...document.querySelectorAll('.photo-card')];
          return cards.length > 0
            && cards.every((card) => card.dataset.mediaType === 'video')
            && cards.some((card) => card.dataset.photoId === expectedId);
        }""",
        arg=video_id,
        timeout=30_000,
    )
    page.locator(f'.photo-card[data-photo-id="{video_id}"] .photo-card__open').click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        """() => {
          const video = document.querySelector('#lightbox-video');
          return video && video.videoWidth > 0 && video.videoHeight > 0
            && video.getBoundingClientRect().width > 0
            && video.getBoundingClientRect().height > 0;
        }""",
        timeout=60_000,
    )
    result = page.evaluate(
        """() => {
          const video = document.querySelector('#lightbox-video');
          const image = document.querySelector('#lightbox-image');
          const media = document.querySelector('.lightbox-media');
          const frame = document.querySelector('#lightbox-image-frame');
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height};
          };
          const videoBox = rect(video);
          const mediaBox = rect(media);
          return {
            controls: video.controls,
            playsInline: video.playsInline,
            preload: video.preload,
            autoplay: video.autoplay,
            paused: video.paused,
            poster: video.poster,
            src: video.currentSrc || video.src,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight,
            videoBox,
            mediaBox,
            imageHidden: image.hidden,
            frameTouchAction: getComputedStyle(frame).touchAction,
            position: document.querySelector('#lightbox-position')?.textContent || '',
          };
        }"""
    )
    assert result["controls"] and result["playsInline"], result
    assert result["preload"] == "metadata" and not result["autoplay"], result
    assert result["paused"], result
    assert result["poster"], result
    assert result["poster"].endswith(preview_url), result
    assert result["src"].endswith(f"/{video_id}/stream"), result
    assert result["imageHidden"], result
    assert result["videoBox"]["left"] >= result["mediaBox"]["left"] - 1, result
    assert result["videoBox"]["right"] <= result["mediaBox"]["right"] + 1, result
    assert result["videoBox"]["top"] >= result["mediaBox"]["top"] - 1, result
    assert result["videoBox"]["bottom"] <= result["mediaBox"]["bottom"] + 1, result
    assert abs(
        result["videoBox"]["width"] / result["videoBox"]["height"]
        - result["videoWidth"] / result["videoHeight"]
    ) < 0.02, result
    return result


def test_live_videos(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    response = live_video_payload(page)
    assert response["status"] == 200, response
    payload = response["payload"]
    items = payload.get("items", [])
    assert_video_contract(items)
    first = items[0]

    result = inspect_video_lightbox(
        page,
        str(first["id"]),
        first["preview_url"],
        has_next=len(items) > 1,
    )
    duration_text = page.locator(
        f'.photo-card[data-photo-id="{first["id"]}"] .photo-card__duration'
    ).text_content()
    if first.get("duration") is not None:
        assert duration_text and ":" in duration_text, duration_text
    assert page.locator(
        f'.photo-card[data-photo-id="{first["id"]}"] .photo-card__play'
    ).count() == 1

    position_before_key = page.locator("#lightbox-position").text_content()
    page.locator("#lightbox-video").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#lightbox-position").text_content() == position_before_key

    page.locator("#lightbox-video").dispatch_event(
        "pointerdown", {"clientX": 300, "clientY": 300, "pointerId": 1}
    )
    page.locator("#lightbox-video").dispatch_event(
        "pointerup", {"clientX": 80, "clientY": 300, "pointerId": 1}
    )
    assert page.locator("#lightbox-position").text_content() == position_before_key

    navigation = None
    if len(items) > 1:
        page.locator("#lightbox-video").evaluate(
            """(video) => {
              const pause = video.pause.bind(video);
              const load = video.load.bind(video);
              video.__pauseCalls = 0;
              video.__loadCalls = 0;
              video.pause = () => { video.__pauseCalls += 1; return pause(); };
              video.load = () => { video.__loadCalls += 1; return load(); };
            }"""
        )
        page.locator("#lightbox-next").click()
        page.wait_for_function(
            "(total) => document.querySelector('#lightbox-position').textContent === '2 / ' + total",
            arg=len(page.locator(".photo-card").all()),
        )
        page.wait_for_function(
            """() => {
              const video = document.querySelector('#lightbox-video');
              return video && video.videoWidth > 0 && video.videoHeight > 0
            && video.getBoundingClientRect().width > 0
            && video.getBoundingClientRect().height > 0;
            }""",
            timeout=60_000,
        )
        navigation = page.evaluate(
            """() => {
              const video = document.querySelector('#lightbox-video');
              return {pauseCalls: video.__pauseCalls || 0, loadCalls: video.__loadCalls || 0};
            }"""
        )
        assert navigation["pauseCalls"] > 0 and navigation["loadCalls"] > 0, navigation

    page.locator("#lightbox-video").evaluate(
        """async (video) => {
          video.currentTime = 0;
          await video.play();
        }"""
    )
    page.wait_for_function(
        "document.querySelector('#lightbox-video').currentTime > 0.2",
        timeout=15_000,
    )
    page.locator("#lightbox-close").click()
    page.wait_for_timeout(120)
    reset = page.evaluate(
        """() => {
          const video = document.querySelector('#lightbox-video');
          return {
            open: document.querySelector('#lightbox')?.open || false,
            src: video.getAttribute('src'),
            currentTime: video.currentTime,
            paused: video.paused,
          };
        }"""
    )
    assert reset == {"open": False, "src": None, "currentTime": 0, "paused": True}, reset

    with page.expect_request(
        re.compile(r"/api/photos\?[^#]*media_type=photo(?:&|$)"), timeout=30_000
    ):
        page.locator("#media-type-filter").select_option("photo")
    page.wait_for_function(
        """() => {
          const cards = [...document.querySelectorAll('.photo-card')];
          return cards.length > 0 && cards.every((card) => card.dataset.mediaType === 'photo');
        }""",
        timeout=30_000,
    )
    page.locator(".photo-card__open").first.click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "document.querySelector('#lightbox-image').naturalWidth > 0", timeout=30_000
    )
    vice_versa = page.evaluate(
        """() => ({
          imageHidden: document.querySelector('#lightbox-image').hidden,
          videoHidden: document.querySelector('#lightbox-video').hidden,
        })"""
    )
    assert vice_versa == {"imageHidden": False, "videoHidden": True}, vice_versa
    page.locator("#lightbox-close").click()
    return {
        "contractItems": len(items),
        "firstVideo": first,
        "lightbox": result,
        "afterClose": reset,
        "photoViceVersa": vice_versa,
    }


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        result = test_live_videos(page)
        context.close()
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
