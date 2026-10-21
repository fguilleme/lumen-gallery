"""Browser regression for the staged touch-rating UI.

Modes
-----
``RATING_TEST_MODE=stub`` (default) is the initial TDD/stub test.  It keeps the
real GET gallery but intercepts PATCH /api/photos/{id}/rating and rating-filter
GETs in Playwright, so it does not change a user's live ratings.  Use it while
the parent backend is not deployed.

``RATING_TEST_MODE=parentlive`` runs against the deployed ratings backend.  It
snapshots every ID it changes and restores the exact previous rating in a
finally block.  It is deliberately not an iPhone/Safari test; Chromium touch
emulation is the evidence collected here.

Run from the repository root with, for example:
    uv run --no-project --with playwright python \
      qa/staging/ratings-static/tests/rating_regression.py
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_URL = os.environ.get(
    "LUMEN_BASE_URL", "https://arcturus.tail38ed6c.ts.net:9443"
)
MODE = os.environ.get("RATING_TEST_MODE", "stub").strip().lower()
if MODE not in {"stub", "parentlive"}:
    raise SystemExit("RATING_TEST_MODE must be stub or parentlive")


def normalise_rating(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number not in range(5):
        return 0
    if isinstance(value, int):
        return number
    return number if str(number) == str(value).strip() else 0


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
    page.locator(".photo-card .rating-control").first.wait_for(timeout=30_000)


def card_selector(item_id: str) -> str:
    return f'.photo-card[data-photo-id="{item_id}"]'


def rating_selector(item_id: str, target: str = "card") -> str:
    if target == "viewer":
        return f'#lightbox .rating-control--viewer[data-photo-id="{item_id}"]'
    return f'{card_selector(item_id)} .rating-control--{target}'


def card_rating(page: Page, item_id: str, target: str = "card") -> int:
    return int(page.locator(rating_selector(item_id, target)).get_attribute("data-rating") or "0")


def click_rating(page: Page, item_id: str, value: int, target: str = "card") -> None:
    assert value in range(1, 5)
    locator = page.locator(f'{rating_selector(item_id, target)} button[data-rating-value="{value}"]')
    assert locator.count() == 1
    with page.expect_response(re.compile(rf"/api/photos/{re.escape(str(item_id))}/rating(?:\?|$)")) as response_info:
        locator.click()
    response = response_info.value
    assert response.status == 200, {"status": response.status, "body": response.text()}
    request_payload = response.request.post_data_json
    assert request_payload == {"rating": value}, request_payload
    page.wait_for_function(
        """({selector, value}) => {
          const control = document.querySelector(selector);
          return control && control.dataset.rating === String(value);
        }""",
        arg={"selector": rating_selector(item_id, target), "value": value},
        timeout=10_000,
    )


def wait_for_rating_patch(page: Page, item_id: str, value: int) -> None:
    page.wait_for_function(
        """({id, value}) => [...document.querySelectorAll('.rating-control')]
          .filter((control) => control.dataset.photoId === id)
          .every((control) => control.dataset.rating === String(value)
            && control.querySelectorAll('.rating-control__star.is-filled').length === value)""",
        arg={"id": str(item_id), "value": value},
        timeout=10_000,
    )


def close_lightbox(page: Page) -> None:
    if page.locator("#lightbox").get_attribute("open") is not None:
        page.locator("#lightbox-close").click()
        page.locator("#lightbox").wait_for(state="hidden", timeout=10_000)


def patch_live_rating(page: Page, item_id: str, rating: int) -> None:
    result = page.evaluate(
        """async ({id, rating}) => {
          const response = await fetch(`/api/photos/${encodeURIComponent(id)}/rating`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rating}),
          });
          return {status: response.status, body: await response.text()};
        }""",
        {"id": str(item_id), "rating": rating},
    )
    if result["status"] != 200:
        raise AssertionError(f"restore rating {item_id} -> {rating} failed: {result}")


def stub_route_factory(
    items: list[dict[str, Any]],
    ratings: dict[str, int],
    patch_log: list[dict[str, Any]],
):
    item_by_id = {str(item["id"]): dict(item) for item in items if item.get("id") is not None}
    for item_id, item in item_by_id.items():
        ratings.setdefault(item_id, normalise_rating(item.get("rating", 0)))

    def handler(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path.endswith("/rating") and request.method == "PATCH":
            item_id = parsed.path.rsplit("/", 2)[-2]
            payload = json.loads(request.post_data or "{}")
            value = int(payload.get("rating", -1))
            if value not in range(5) or item_id not in item_by_id:
                route.fulfill(status=422, content_type="application/json", body=json.dumps({"error": "invalid rating"}))
                return
            ratings[item_id] = value
            patch_log.append({"id": item_id, "rating": value})
            updated = {**item_by_id[item_id], "rating": value}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(updated))
            return

        if parsed.path.endswith("/photos") and request.method == "GET":
            query = parse_qs(parsed.query)
            result = list(item_by_id.values())
            if "rating" in query:
                requested = int(query["rating"][0])
                result = [item for item in result if ratings.get(str(item["id"]), 0) == requested]
            sort = query.get("sort", ["date_desc"])[0]
            if sort in {"rating_asc", "rating_desc"}:
                result.sort(
                    key=lambda item: (
                        ratings.get(str(item["id"]), 0),
                        str(item.get("name", "")),
                    ),
                    reverse=sort == "rating_desc",
                )
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["24"])[0])
            page_items = result[offset : offset + limit]
            page_items = [{**item, "rating": ratings.get(str(item["id"]), 0)} for item in page_items]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"items": page_items, "total": len(result)}),
            )
            return

        route.continue_()

    return handler


def select_rating_filter(page: Page, value: str) -> None:
    page.locator("#filter-details").evaluate("element => { element.open = true; }")
    with page.expect_request(re.compile(r"/api/photos\?[^#]*rating=" + re.escape(value))):
        page.locator("#rating-filter").select_option(value)
    page.wait_for_timeout(250)


def inspect_mobile(page: Page, item_id: str) -> dict[str, Any]:
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.locator(".photo-card .rating-control").first.wait_for(timeout=60_000)
    page.evaluate("window.scrollTo({top: 600, left: 0, behavior: 'instant'})")
    page.wait_for_timeout(120)
    before = page.evaluate(
        """(id) => {
          const card = document.querySelector(`[data-photo-id="${id}"]`);
          const control = card?.querySelector('.rating-control');
          const frame = card?.querySelector('.photo-card__frame');
          const play = card?.querySelector('.photo-card__play');
          const duration = card?.querySelector('.photo-card__duration');
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height};
          };
          return {
            scrollY: window.scrollY,
            scrollWidth: document.documentElement.scrollWidth,
            viewportWidth: innerWidth,
            card: card ? rect(card) : null,
            frame: frame ? rect(frame) : null,
            rating: control ? rect(control) : null,
            play: play ? rect(play) : null,
            duration: duration ? rect(duration) : null,
            ratingHeight: control ? control.getBoundingClientRect().height : 0,
            nestedRatingButtons: document.querySelectorAll('.photo-card__open .rating-control').length,
          };
        }""",
        item_id,
    )
    assert before["scrollWidth"] == before["viewportWidth"], before
    assert before["nestedRatingButtons"] == 0, before
    assert before["ratingHeight"] >= 40, before
    assert before["rating"]["left"] >= before["frame"]["left"] - 1, before
    assert before["rating"]["right"] <= before["frame"]["right"] + 1, before
    if before["play"]:
        assert before["rating"]["right"] <= before["play"]["left"] or before["rating"]["top"] >= before["play"]["bottom"], before
    if before["duration"]:
        assert before["rating"]["bottom"] <= before["duration"]["top"] or before["rating"]["top"] >= before["duration"]["bottom"], before
    scroll_before = before["scrollY"]
    current = card_rating(page, item_id)
    next_value = 3 if current != 3 else 2
    click_rating(page, item_id, next_value)
    wait_for_rating_patch(page, item_id, next_value)
    page.wait_for_timeout(120)
    after = page.evaluate("window.scrollY")
    assert abs(after - scroll_before) <= 24, {"before": before, "after": after}
    assert page.locator("#lightbox").get_attribute("open") is None
    return {"before": before, "scrollYAfterRating": after, "rating": next_value}


def inspect_video_controls(page: Page) -> dict[str, Any] | None:
    video_cards = page.locator('.photo-card[data-media-type="video"]')
    if video_cards.count() == 0:
        return None
    video_cards.first.locator(".photo-card__open").click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    page.locator("#lightbox-video").wait_for(state="visible", timeout=10_000)
    result = page.evaluate(
        """() => {
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height};
          };
          const frame = document.querySelector('#lightbox-image-frame');
          const video = document.querySelector('#lightbox-video');
          const rating = document.querySelector('#lightbox-rating .rating-control');
          const previous = document.querySelector('#lightbox-previous');
          const next = document.querySelector('#lightbox-next');
          return {
            controls: video?.controls || false,
            shareButtons: document.querySelectorAll('#lightbox-share').length,
            touchAction: getComputedStyle(frame).touchAction,
            frame: frame ? rect(frame) : null,
            video: video ? rect(video) : null,
            rating: rating ? rect(rating) : null,
            previous: previous ? rect(previous) : null,
            next: next ? rect(next) : null,
          };
        }"""
    )
    assert result["controls"] and result["shareButtons"] == 1, result
    assert result["touchAction"] in {"pan-y", "auto"}, result
    assert result["rating"]["bottom"] <= result["video"]["bottom"] - 44, result
    assert result["rating"]["left"] > result["previous"]["right"] + 2, result
    assert result["rating"]["right"] < result["next"]["left"] - 2, result
    close_lightbox(page)
    return result


def run_rating_regression(page: Page) -> dict[str, Any]:
    wait_for_gallery(page)
    items = live_items(page)
    assert items, "live gallery has no media"
    target = items[0]
    target_id = str(target["id"])
    original_rating = normalise_rating(target.get("rating", 0))
    changed_ids = {target_id: original_rating}
    result: dict[str, Any] = {"mode": MODE, "target": target_id}

    try:
        assert page.locator(".photo-card__open .rating-control").count() == 0
        assert page.locator("#sort-select option[value='rating_asc']").count() == 1
        assert page.locator("#sort-select option[value='rating_desc']").count() == 1
        assert page.locator("#rating-filter option").all_text_contents() == [
            "Toutes", "0 étoile", "1 étoile", "2 étoiles", "3 étoiles", "4 étoiles"
        ]

        current = card_rating(page, target_id)
        first_value = 4 if current != 4 else 3
        click_rating(page, target_id, first_value)
        wait_for_rating_patch(page, target_id, first_value)
        assert page.locator("#lightbox").get_attribute("open") is None
        result["cardTouch"] = {"rating": first_value, "lightboxOpened": False}

        page.locator(f'{card_selector(target_id)} .photo-card__open').click()
        page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
        assert page.locator("#lightbox-rating .rating-control").count() == 1
        assert card_rating(page, target_id, "card") == card_rating(page, target_id, "viewer") == first_value
        second_value = 2 if first_value != 2 else 1
        click_rating(page, target_id, second_value, "viewer")
        wait_for_rating_patch(page, target_id, second_value)
        assert card_rating(page, target_id, "card") == card_rating(page, target_id, "viewer") == second_value
        assert page.locator("#lightbox-share").count() == 1
        close_lightbox(page)
        result["viewerSync"] = {"rating": second_value, "sharePresent": True}

        page.reload(wait_until="domcontentloaded")
        page.locator(".photo-card .rating-control").first.wait_for(timeout=60_000)
        persisted_rating = card_rating(page, target_id)
        assert persisted_rating == second_value, {"expected": second_value, "actual": persisted_rating, "target": target_id}
        result["reloadPersisted"] = True

        keyboard_control = page.locator(rating_selector(target_id))
        keyboard_control.focus()
        with page.expect_response(re.compile(rf"/api/photos/{re.escape(target_id)}/rating(?:\?|$)")) as clear_response:
            page.keyboard.press("Delete")
        assert clear_response.value.status == 200
        wait_for_rating_patch(page, target_id, 0)
        assert card_rating(page, target_id) == 0
        result["keyboardClear"] = True

        # The selected final star clears the rating, without opening the viewer.
        page.locator(f'{rating_selector(target_id)} button[data-rating-value="{second_value}"]').click()
        wait_for_rating_patch(page, target_id, second_value)
        page.locator(f'{rating_selector(target_id)} button[data-rating-value="{second_value}"]').click()
        wait_for_rating_patch(page, target_id, 0)
        assert card_rating(page, target_id) == 0
        assert page.locator("#lightbox").get_attribute("open") is None
        result["toggleZero"] = True

        # Make one deterministic non-zero item for exact zero/non-zero filter checks.
        page.locator(f'{rating_selector(target_id)} button[data-rating-value="4"]').click()
        wait_for_rating_patch(page, target_id, 4)
        select_rating_filter(page, "0")
        assert all(card_rating(page, item_id) == 0 for item_id in page.locator(".photo-card").evaluate_all("cards => cards.map(card => card.dataset.photoId)"))
        select_rating_filter(page, "4")
        assert any(card_rating(page, item_id) == 4 for item_id in page.locator(".photo-card").evaluate_all("cards => cards.map(card => card.dataset.photoId)"))
        result["filters"] = {"zero": True, "nonzero": True, "zeroQueryPreserved": True}

        with page.expect_request(re.compile(r"/api/photos\?[^#]*sort=rating_asc")):
            page.locator("#sort-select").select_option("rating_asc")
        with page.expect_request(re.compile(r"/api/photos\?[^#]*sort=rating_desc")):
            page.locator("#sort-select").select_option("rating_desc")
        result["sorts"] = ["rating_asc", "rating_desc"]

        page.locator("#rating-filter").select_option("")
        result["mobile"] = inspect_mobile(page, target_id)
        result["video"] = inspect_video_controls(page)

        select_rating_filter(page, "4")
        with page.expect_request(re.compile(r"/api/photos\?[^#]*imported_only=true")) as imported_request:
            page.locator("#imported-only-filter").check()
        assert "rating=" not in imported_request.value.url
        assert page.locator("#rating-filter").input_value() == ""
        result["importedClearsRating"] = True
        return result
    finally:
        if MODE == "parentlive":
            for item_id, rating in changed_ids.items():
                patch_live_rating(page, item_id, rating)
            restored = page.evaluate(
                """async (id) => {
                  const response = await fetch('/api/photos?sort=date_desc&offset=0&limit=200');
                  const payload = await response.json();
                  const item = (payload.items || []).find((entry) => String(entry.id) === String(id));
                  return item ? item.rating ?? 0 : null;
                }""",
                target_id,
            )
            assert normalise_rating(restored) == original_rating, {
                "id": target_id,
                "expected": original_rating,
                "actual": restored,
            }


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=1,
        )
        page = context.new_page()
        stub_ratings: dict[str, int] = {}
        patch_log: list[dict[str, Any]] = []
        if MODE == "stub":
            # The route is installed after the initial GET snapshot so the
            # filter emulation has real IDs/media from the deployed gallery.
            wait_for_gallery(page)
            snapshot = live_items(page)
            handler = stub_route_factory(snapshot, stub_ratings, patch_log)
            page.route("**/api/photos/**", handler)
            page.route("**/api/photos?*", handler)
            result = run_rating_regression(page)
            result["stubPatchCalls"] = patch_log
        else:
            result = run_rating_regression(page)
        context.close()
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
