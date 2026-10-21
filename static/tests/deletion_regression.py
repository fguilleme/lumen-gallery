"""Stubbed browser contract test for the staged delayed-trash UI.

This test is intentionally safe before the deletion worker is deployed.  It runs
against a local static server and intercepts every /api request with a small
in-memory fixture; it never schedules a real collection or touches live media.
Run with:
    uv run --no-project --with playwright python \
      qa/staging/deletion-static/tests/deletion_regression.py
"""

from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, sync_playwright


ROOT = Path("/home/francois/projects/lumen-gallery")
STAGE = ROOT / "qa/staging/deletion-static"
MODE = os.environ.get("DELETION_TEST_MODE", "stub").strip().lower()
if MODE != "stub":
    raise SystemExit("DELETION_TEST_MODE must remain stub until a backend fixture is available")


DUE_AT = "2026-11-20T10:00:00Z"


def fixture_items() -> list[dict[str, object]]:
    return [
        {
            "id": "zero-visible",
            "name": "visible-zero.jpg",
            "rating": 0,
            "date": "2026-10-21T10:00:00Z",
            "format": "JPEG",
            "media_type": "photo",
            "width": 1200,
            "height": 800,
            "deletion_scheduled_at": None,
            "delete_after": None,
        },
        {
            "id": "zero-hidden",
            "name": "hidden-zero.mp4",
            "rating": 0,
            "date": "2026-10-20T10:00:00Z",
            "format": "MP4",
            "media_type": "video",
            "width": 800,
            "height": 1200,
            "deletion_scheduled_at": None,
            "delete_after": None,
        },
        {
            "id": "rated",
            "name": "rated.jpg",
            "rating": 2,
            "date": "2026-10-19T10:00:00Z",
            "format": "JPEG",
            "media_type": "photo",
            "width": 1000,
            "height": 1000,
            "deletion_scheduled_at": None,
            "delete_after": None,
        },
    ]


def json_response(route: Route, payload: object, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload),
    )


def stub_api_factory(items: list[dict[str, object]], calls: list[dict[str, object]]):
    def handler(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        query = parse_qs(parsed.query)
        path = parsed.path
        calls.append({"method": request.method, "path": path, "query": parsed.query})

        if path == "/api/session" and request.method == "GET":
            json_response(route, {"authenticated": True, "auth_required": False})
            return
        if path == "/api/status" and request.method == "GET":
            json_response(route, {"running": False, "errors": 0})
            return
        if path == "/api/facets" and request.method == "GET":
            json_response(route, {"cameras": [], "lenses": [], "formats": ["JPEG", "MP4"]})
            return
        if path == "/api/deletions/status" and request.method == "GET":
            scheduled = sum(
                1 for item in items if item.get("deletion_scheduled_at") is not None
            )
            json_response(route, {"scheduled": scheduled, "trashed": 0})
            return
        if path == "/api/deletions/schedule-zero" and request.method == "POST":
            payload = json.loads(request.post_data or "{}")
            assert payload == {}, payload
            newly_scheduled = 0
            already_scheduled = 0
            for item in items:
                if item.get("rating") != 0:
                    continue
                if item.get("deletion_scheduled_at") is not None:
                    already_scheduled += 1
                    continue
                item["deletion_scheduled_at"] = "2026-10-21T10:00:00Z"
                item["delete_after"] = DUE_AT
                item["deletion_status"] = "scheduled"
                newly_scheduled += 1
            json_response(
                route,
                {
                    "scheduled": newly_scheduled,
                    "already_scheduled": already_scheduled,
                    "due_at": DUE_AT,
                },
            )
            return
        if path == "/api/photos" and request.method == "GET":
            result = [dict(item) for item in items]
            if "rating" in query:
                result = [item for item in result if str(item["rating"]) == query["rating"][0]]
            if query.get("deletion_status") == ["scheduled"]:
                result = [
                    item for item in result if item.get("deletion_scheduled_at") is not None
                ]
            if query.get("media_type"):
                result = [
                    item for item in result if item.get("media_type") == query["media_type"][0]
                ]
            if "q" in query:
                needle = query["q"][0].lower()
                result = [item for item in result if needle in str(item["name"]).lower()]
            result.sort(key=lambda item: str(item["date"]), reverse=True)
            total = len(result)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["24"])[0])
            json_response(route, {"items": result[offset : offset + limit], "total": total})
            return
        if path.startswith("/api/photos/") and path.endswith("/rating") and request.method == "PATCH":
            item_id = path.split("/")[-2]
            payload = json.loads(request.post_data or "{}")
            item = next(item for item in items if str(item["id"]) == item_id)
            item["rating"] = int(payload["rating"])
            if item["rating"] >= 1:
                item["deletion_scheduled_at"] = None
                item["delete_after"] = None
                item.pop("deletion_status", None)
            json_response(route, dict(item))
            return
        if path.startswith("/api/photos/") and request.method == "GET":
            item_id = path.rsplit("/", 1)[-1]
            item = next(item for item in items if str(item["id"]) == item_id)
            json_response(route, dict(item))
            return

        json_response(route, {"error": f"stub route missing: {request.method} {path}"}, 404)

    return handler


def stage_asset_factory():
    def handler(route: Route) -> None:
        parsed = urlparse(route.request.url)
        if parsed.path == "/static/app.js":
            route.fulfill(
                status=200,
                content_type="text/javascript; charset=utf-8",
                body=(STAGE / "app.js").read_text(encoding="utf-8"),
            )
            return
        if parsed.path == "/static/style.css":
            route.fulfill(
                status=200,
                content_type="text/css; charset=utf-8",
                body=(STAGE / "style.css").read_text(encoding="utf-8"),
            )
            return
        route.continue_()

    return handler


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def start_static_server() -> ThreadingHTTPServer:
    handler = lambda *args, **kwargs: QuietStaticHandler(*args, directory=str(STAGE), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_for_gallery(page: Page) -> None:
    page.locator("#app-view").wait_for(state="visible", timeout=10_000)
    page.locator(".photo-card").first.wait_for(timeout=10_000)
    page.locator("#schedule-zero-button").wait_for(timeout=10_000)


def inspect_responsive_card_overlays(page: Page) -> dict[str, object]:
    results: dict[str, object] = {}
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(120)
        result = page.evaluate(
            """() => {
              const overlap = (first, second) => first && second
                && first.left < second.right && first.right > second.left
                && first.top < second.bottom && first.bottom > second.top;
              const rect = (element) => {
                if (!element) return null;
                const box = element.getBoundingClientRect();
                return {left: box.left, right: box.right, top: box.top, bottom: box.bottom};
              };
              return {
                viewport: innerWidth,
                scrollWidth: document.documentElement.scrollWidth,
                cards: [...document.querySelectorAll('.photo-card')].map((card) => {
                  const frame = rect(card.querySelector('.photo-card__frame'));
                  const badge = rect(card.querySelector('.photo-card__deletion'));
                  const rating = rect(card.querySelector('.rating-control--card'));
                  const play = rect(card.querySelector('.photo-card__play'));
                  return {frame, badge, rating, play, badgeRatingOverlap: overlap(badge, rating), badgePlayOverlap: overlap(badge, play)};
                }),
              };
            }"""
        )
        assert result["scrollWidth"] <= width + 1, result
        for card in result["cards"]:
            if card["badge"]:
                assert card["frame"]["left"] <= card["badge"]["left"] + 1, result
                assert card["badge"]["right"] <= card["frame"]["right"] + 1, result
                assert not card["badgeRatingOverlap"] and not card["badgePlayOverlap"], result
        results[str(width)] = result
    return results


def test_delayed_trash_contract(page: Page) -> dict[str, object]:
    wait_for_gallery(page)
    assert page.locator("#archive-title").count() == 0
    assert page.locator("#search-input").count() == 0
    gallery_text = page.locator("#app-view").inner_text()
    assert "Collection privée" not in gallery_text
    assert "Lumière" not in gallery_text
    assert page.locator("#schedule-zero-button").is_enabled()
    assert page.locator("#zero-deletion-count").inner_text() == "2"
    assert page.locator(".photo-card .rating-control").first.evaluate(
        "element => getComputedStyle(element).backgroundColor"
    ) == "rgba(10, 12, 14, 0.35)"

    # A visible menu filter must not change the all-catalogue zero count.
    page.locator("#media-type-filter").select_option("photo")
    page.wait_for_timeout(400)
    assert page.locator(".photo-card").count() == 2
    assert page.locator("#zero-deletion-count").inner_text() == "2"

    # Imported-only is a mutually exclusive shortcut and clears menu filters.
    page.locator("#filter-details").evaluate("element => { element.open = true; }")
    page.locator("#rating-filter").select_option("0")
    page.locator("#filter-details").evaluate("element => { element.open = false; }")
    page.locator("#imported-only-filter").check()
    page.wait_for_timeout(400)
    assert page.locator("#media-type-filter").input_value() == ""
    assert page.locator("#rating-filter").input_value() == ""
    assert page.locator("#deletion-status-filter").input_value() == ""
    page.locator("#imported-only-filter").uncheck()
    page.locator("#media-type-filter").select_option("photo")
    page.wait_for_timeout(400)
    assert page.locator(".photo-card").count() == 2

    # First cancel the confirmation; no scheduling request may be emitted.
    page.locator("#schedule-zero-button").click()
    page.locator("#schedule-zero-dialog").wait_for(state="visible", timeout=10_000)
    message = page.locator("#schedule-zero-message").inner_text()
    assert message == (
        "Tous les 2 médias à 0 étoile, y compris hors des filtres actuels, "
        "seront placés en corbeille dans un mois. Remettre une étoile annule "
        "la programmation. Aucun fichier supprimé définitivement."
    )
    page.locator("#schedule-zero-cancel").click()
    assert page.locator("#schedule-zero-dialog").get_attribute("open") is None

    page.locator("#schedule-zero-button").click()
    page.locator("#schedule-zero-confirm").click()
    page.wait_for_function(
        "() => document.querySelector('#deletion-summary')?.textContent.includes('2 programmés')",
        timeout=10_000,
    )

    page.locator("#media-type-filter").select_option("")
    page.wait_for_timeout(400)
    page.locator("#filter-details").evaluate("element => { element.open = true; }")
    page.locator("#deletion-status-filter").select_option("scheduled")
    page.locator("#filter-details").evaluate("element => { element.open = false; }")
    page.locator('.photo-card[data-photo-id="zero-visible"] .photo-card__open').click()
    page.locator("#lightbox").wait_for(state="visible", timeout=10_000)
    assert page.locator("#lightbox .rating-control").evaluate(
        "element => getComputedStyle(element).backgroundColor"
    ) == "rgba(10, 12, 14, 0.35)"
    assert "20 nov." in page.locator("#lightbox-deletion-status").inner_text()
    page.locator('#lightbox-rating button[data-rating-value="1"]').click()
    page.locator("#lightbox").locator("#lightbox-close").click()
    page.wait_for_function(
        "() => !document.querySelector('.photo-card[data-photo-id=\\\"zero-visible\\\"]')",
        timeout=10_000,
    )
    assert page.locator('.photo-card[data-photo-id="zero-visible"]').count() == 0
    assert page.locator("#schedule-zero-button").is_enabled()
    responsive = inspect_responsive_card_overlays(page)
    return {
        "confirmation": message,
        "scheduled_filter_remaining": page.locator(".photo-card").count(),
        "responsive": responsive,
    }


def main() -> None:
    server = start_static_server()
    try:
        items = fixture_items()
        calls: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
            try:
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.route("**/static/**", stage_asset_factory())
                page.route("**/api/**", stub_api_factory(items, calls))
                page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
                result = test_delayed_trash_contract(page)
                assert not page_errors, page_errors
                schedule_calls = [
                    call for call in calls if call["path"] == "/api/deletions/schedule-zero"
                ]
                assert len(schedule_calls) == 1, schedule_calls
                rating_calls = [call for call in calls if str(call["path"]).endswith("/rating")]
                assert rating_calls, calls
                assert not [call for call in calls if call["path"] == "/api/deletions/delete"]
                result["schedule_posts"] = len(schedule_calls)
                result["rating_patches"] = len(rating_calls)
            finally:
                context.close()
                browser.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
