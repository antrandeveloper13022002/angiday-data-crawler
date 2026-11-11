"""GrabFood restaurant menus through a headed Chromium, signed out (DC-09 spike, 2026-11-11):

    python -m crawler grab menus --id 5-C75BL6WCMANWLT [--ids-file F] [--refresh]

One page load per restaurant: the page asks portal.grab.com's /foodweb/guest/v2/merchants/<id> for the restaurant and
its whole menu, and that answer is kept. Headless Chromium gets CloudFront's 403 page; a headed one gets through.
Grab's WAF answers 429 when pages come too fast — handled as a block (performance.md).

Read-only: the browser only navigates (security.md → Money Safety). No account, no cart, no delivery address.
"""

import argparse
import logging
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from crawler import core

SOURCE = "grab"
ROOT = Path(__file__).resolve().parents[2]
DONE_INDEX = ROOT / "data" / "state" / "grab-restaurants.json"  # outside raw/: readers glob raw/grab/*.json
PAGE_URL = "https://food.grab.com/vn/vi/restaurant/online-delivery/"  # vi: menu names as the restaurant wrote them
MERCHANT_API = "portal.grab.com/foodweb/guest/v2/merchants/"
GRAB_ID = re.compile(r"\d-[A-Z0-9]{10,20}")  # 5-C75BL6WCMANWLT; checked before it goes into a URL
DISCOUNT = re.compile(r"-?(\d{1,3})%")
MAX_RESTAURANTS = 200  # per run (performance.md)
MAX_BLOCKS = 3

log = logging.getLogger(__name__)


class Blocked(Exception):
    """Grab stopped answering: the run ends, saved as FAILED with what it has (performance.md)."""


# ---- mapping: Grab's merchant → the polyglot-data~grabfood-scraper item (docs/data/output-contract.md) ----


def apify_price(minor: object) -> int | float | None:
    """The Apify actor's price unit: Grab's amount in đồng / 100 (40.000đ → 400), so old and new files compare."""
    if not isinstance(minor, int) or isinstance(minor, bool):
        return None
    return minor // 100 if minor % 100 == 0 else minor / 100


def menu_item(category: dict, native: dict) -> dict:
    price, discounted = native.get("priceInMinorUnit"), native.get("discountedPriceInMin")
    pct = DISCOUNT.fullmatch(str(native.get("discountPercentage") or ""))
    return {
        "category": category.get("name"),
        "name": native.get("name"),
        "description": native.get("description") or None,
        "price": apify_price(price),
        "discountedPrice": apify_price(discounted) if discounted != price else None,
        "discountPct": int(pct.group(1)) if pct else None,
        "available": native.get("available"),  # Grab leaves it out on some items: null, not guessed
        "imageUrl": native.get("imgHref") or None,
    }


def to_item(merchant: dict, url: str, scraped_at: str) -> dict:
    """What Grab didn't send is null, never made up (data-guideline.md). The restaurant page carries no delivery
    fee, ETA, distance or promo labels without a delivery address, so those stay null."""
    address = merchant.get("address") or {}
    cuisine = merchant.get("cuisine")
    menu = [
        menu_item(category, native)
        for category in (merchant.get("menu") or {}).get("categories") or []
        for native in category.get("items") or []
    ]
    return {
        "url": url,
        "restaurantId": merchant.get("ID"),
        "name": merchant.get("name"),
        "chainName": merchant.get("chainName"),
        "branchName": None,
        "cuisine": [part.strip() for part in cuisine.split(",") if part.strip()] if isinstance(cuisine, str) else None,
        "rating": merchant.get("rating"),
        "reviewCount": merchant.get("voteCount"),
        "isOpen": None,
        "openingHours": merchant.get("openingHours"),
        "hasPromo": None,
        "promoLabels": None,
        "deliveryFee": None,
        "currency": (merchant.get("currency") or {}).get("code"),
        "estimatedDeliveryMinutes": None,
        "deliveryTimeRange": None,
        "distanceKm": None,
        "address": address.get("combined_address"),
        "city": address.get("city"),
        "postcode": address.get("postcode"),
        "imageUrl": merchant.get("photoHref"),
        "menuItemCount": len(menu),
        "menu": menu,
        "searchLocation": None,
        "country": "vn",
        "scrapedAt": scraped_at,
    }


# ---- input ----


def restaurant_id(line: str) -> str:
    """A Grab restaurant id, or the id at the end of a restaurant link (Google Maps lists these as the website)."""
    candidate = line.strip().split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if not GRAB_ID.fullmatch(candidate):
        raise ValueError(f"not a Grab restaurant id or link: {line!r}")
    return candidate


def read_ids(ids: list[str], files: list[Path]) -> list[str]:
    """--id values, then each file's lines (UTF-8, one id or link per line); blanks dropped, first copy kept."""
    lines = [*ids, *(line for file in files for line in file.read_text(encoding="utf-8").splitlines())]
    return list(dict.fromkeys(restaurant_id(line) for line in lines if line.strip()))


# ---- browser ----


def merchant_of(response) -> dict | None:
    """The response's payload when it carries a restaurant, else None (an empty or non-JSON body)."""
    try:
        payload = response.json()
    except (PlaywrightError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("merchant"), dict) else None


def load_merchant(page, grab_id: str) -> tuple[dict | None, str]:
    """Grab's answer for one restaurant page: (payload, "ok"), or (None, "blocked" | "missing"). The page asks the
    merchant API more than once and the first answer can be empty (DC-09 live run), so every answer is kept and the
    first that carries the restaurant wins."""
    responses = []

    def keep(response):
        if MERCHANT_API + grab_id in response.url and response.request.method == "GET":
            responses.append(response)

    page.on("response", keep)
    try:
        try:
            page.goto(PAGE_URL + grab_id, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightError as error:
            log.warning("%s: page did not load (%s)", grab_id, str(error).splitlines()[0])
        read, deadline = 0, time.time() + 30
        while time.time() < deadline:
            while read < len(responses):
                response = responses[read]
                read += 1
                if response.status in (403, 429):
                    log.warning("%s: HTTP %s from Grab", grab_id, response.status)
                    return None, "blocked"
                payload = merchant_of(response)
                if payload:
                    return payload, "ok"
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", keep)
    if "request could not be satisfied" in page.content().lower():  # CloudFront's 403 page
        log.warning("%s: CloudFront refused the page", grab_id)
        return None, "blocked"
    statuses = [response.status for response in responses]
    log.warning("%s: no restaurant in %d merchant answers %s", grab_id, len(statuses), statuses)
    return None, "missing"


def crawl(page, run: core.Run, ids: list[str]) -> None:
    """Each restaurant in turn, paced (performance.md); everything lands in `run` as it comes."""
    pace, blocks = 1.0, 0
    for number, grab_id in enumerate(ids):
        if number:
            time.sleep(random.uniform(5, 10) * pace)  # between restaurants
        payload, outcome = load_merchant(page, grab_id)
        if outcome == "blocked":
            blocks += 1
            log.warning("%s: blocked (%d of %d in a row)", grab_id, blocks, MAX_BLOCKS)
            if blocks >= MAX_BLOCKS:
                raise Blocked(f"{blocks} restaurants in a row blocked")
            time.sleep(60)
            pace *= 2  # slow down for the rest of the run
            continue
        blocks = 0
        if outcome == "missing":  # gone from Grab, or a one-off: not done, tried again next run
            continue
        scraped_at = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S.000Z}"
        url = PAGE_URL + grab_id
        run.native.append({"label": {"restaurantId": grab_id, "url": url, "scrapedAt": scraped_at}, "payload": payload})
        item = to_item(payload["merchant"], url, scraped_at)
        run.items.append(item)
        core.mark_done(DONE_INDEX, grab_id, item["menuItemCount"], run.id)
        log.info("%s %r: %d menu items", grab_id, item["name"], item["menuItemCount"])


# ---- commands ----


def menus(args: argparse.Namespace) -> int:
    try:
        ids = read_ids(args.id, args.ids_file)
    except (OSError, ValueError) as error:
        log.error("%s", error)
        return 2
    if not ids:
        log.error("no restaurants: give --id and/or --ids-file")
        return 2
    done = {} if args.refresh else core.read_done(DONE_INDEX)
    todo = core.due(ids, done)
    log.info("%d restaurants: %d already done, %d to read", len(ids), len(ids) - len(todo), len(todo))
    if not todo:
        return 0
    if len(todo) > MAX_RESTAURANTS:
        log.warning("%d-restaurant cap: %d wait for the next run", MAX_RESTAURANTS, len(todo) - MAX_RESTAURANTS)
    # input = exactly what this run sets out to read, so a SUCCEEDED run read every one (output-contract.md)
    run_input = {
        "restaurantIds": todo[:MAX_RESTAURANTS],
        "engine": "playwright-chromium headed",
        "session": "signed out",
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, args=["--mute-audio"])  # headless gets CloudFront's 403
        try:
            page = browser.new_context(locale="vi-VN", viewport={"width": 1366, "height": 900}).new_page()
            with core.Run(SOURCE, run_input) as run:
                crawl(page, run, run_input["restaurantIds"])
        except Blocked as error:
            log.error("stopped: %s — what was collected is saved", error)
            return 1
        finally:
            browser.close()
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser(SOURCE, help="GrabFood restaurant menus (headed browser, signed out)")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    menus_parser = commands.add_parser("menus", help="restaurant menus into data/raw/grab/<runId>.json")
    menus_parser.add_argument("--id", action="append", default=[], help="a restaurant id or link (repeatable)")
    menus_parser.add_argument("--ids-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    menus_parser.add_argument("--refresh", action="store_true", help="read restaurants that are already done again")
    menus_parser.set_defaults(run=menus)
