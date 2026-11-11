"""GrabFood restaurant menus through a headed Chromium, signed out (DC-09 spike, 2026-11-11):

    python -m crawler grab search --place "Ăn Vặt Báo @ 10.833706,106.689434" [--places-file F] [--refresh]
    python -m crawler grab menus --id 5-C75BL6WCMANWLT [--ids-file F] [--refresh]

`search` finds a place's restaurant id (DC-10): Grab's search page searches around its own `location` cookie, which
is set to the place's coordinates first — the one exception to "navigate and scroll only" (security.md, owner
2026-11-11). Search is fuzzy and ranked by relevance, so a hit counts only when it is within MAX_MATCH_M and its
name matches (FoodReview's sameShop, ported); matched ids go to data/state/grab-ids.txt for `menus`.

One page load per restaurant: the page asks portal.grab.com's /foodweb/guest/v2/merchants/<id> for the restaurant and
its whole menu, and that answer is kept. Headless Chromium gets CloudFront's 403 page; a headed one gets through.
Grab's WAF answers 429 when pages come too fast — handled as a block (performance.md).

Read-only: the browser only navigates (security.md → Money Safety). No account, no cart, no delivery address.
"""

import argparse
import json
import logging
import random
import re
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

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
SEARCH_URL = "https://food.grab.com/vn/vi/restaurants"
SEARCH_API = "portal.grab.com/foodweb/guest/v2/search"
MATCH_INDEX = ROOT / "data" / "state" / "grab-matches.json"  # place line → its restaurant id, or null
IDS_FILE = ROOT / "data" / "state" / "grab-ids.txt"  # every matched id, the input of `grab menus --ids-file`
CENTRE = re.compile(r"^(.+?)\s+@\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")  # "name @ lat,lng"
MAX_MATCH_M = 300  # FoodReview's APP-42 rule
MAX_SEARCHES = 100  # per run: search is rate-limited harder than restaurant pages (DC-10 spike)

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


# ---- search matching: FoodReview's lib/place-name.ts sameShop, ported (APP-42 rule) ----

# the word lists exactly as in place-name.ts
STOP_WORDS = "quan tiem nha hang restaurant bistro the cua"
GENERIC_WORDS = (
    "banh mi com pho bun hu tieu tiu chao mien lau oc ca phe tra sua che kem nuong chien xao bo ga heo vit ca tom ghe "
    "hai san an vat ngon nha hang restaurant kitchen bistro bar cafe coffee food fast house quan tiem seafood bbq sushi "
    "pizza noodle soup rice the and of saigon sai gon"
)
STOP, GENERIC = set(STOP_WORDS.split()), set(GENERIC_WORDS.split())


def normalize_name(name: str) -> list[str]:
    """Lower-case ASCII words, no diacritics, brackets or stop words: "Quán Phá Lấu Lì (Q1)" → [pha, lau, li]."""
    text = unicodedata.normalize("NFD", re.sub(r"\([^)]*\)", " ", name))
    text = "".join(c for c in text if not unicodedata.combining(c)).replace("đ", "d").replace("Đ", "d").lower()
    return [word for word in re.split(r"[^a-z0-9]+", text) if word and word not in STOP]


def same_shop(first: str, second: str) -> bool:
    """Every distinctive word of the shorter name is in the other ("Aha Food" ↔ "Aha Food - Nguyễn Quý Đức"), or its
    run-together spelling is, when long enough; generic words never make a match."""
    short, other = sorted(
        ([w for w in normalize_name(name) if w not in GENERIC] for name in (first, second)),
        key=lambda w: len("".join(w)),
    )
    joined = "".join(short)
    if len(joined) < 3:
        return False
    return all(word in other for word in short) or (len(joined) >= 4 and joined in "".join(other))


def pick_hit(name: str, hits: list[dict]) -> dict | None:
    """The nearest search hit that is the place: within MAX_MATCH_M (Grab's distance is from the search point, the
    place) and the same name. None when no hit is."""
    good = [
        hit
        for hit in hits
        if GRAB_ID.fullmatch(str(hit.get("id", "")))
        and isinstance(distance_km(hit), float | int)
        and distance_km(hit) * 1000 <= MAX_MATCH_M
        and same_shop(name, (hit.get("address") or {}).get("name") or "")
    ]
    return min(good, key=distance_km, default=None)


def distance_km(hit: dict) -> float | None:
    return (hit.get("merchantBrief") or {}).get("distanceInKm")


def parse_place(line: str) -> tuple[str, float, float]:
    """ "name @ lat,lng" → (name, lat, lng); anything else is refused (the coordinates go into a cookie)."""
    match = CENTRE.match(" ".join(line.split()))
    if not match:
        raise ValueError(f"not 'name @ lat,lng': {line!r}")
    name, lat, lng = match.group(1), float(match.group(2)), float(match.group(3))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"coordinates out of range: {line!r}")
    return name, lat, lng


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


def set_location(context, cookie: dict, lat: float, lng: float) -> None:
    """Grab's own `location` cookie, moved to the place (security.md exception). An empty address makes the page fall
    back to its IP location (DC-10 spike), so the coordinates stand in for it."""
    location = {**json.loads(unquote(cookie["value"])), "latitude": lat, "longitude": lng, "isAccurate": True}
    location["address"] = f"{lat},{lng}"
    context.clear_cookies(name="location")
    keep = ("domain", "path", "secure", "httpOnly", "sameSite", "expires")
    value = quote(json.dumps(location, separators=(",", ":")))
    context.add_cookies([{**{key: cookie[key] for key in keep if key in cookie}, "name": "location", "value": value}])


def search_place(page, name: str, pages: int, pace: float) -> tuple[list[dict], dict | None, str]:
    """Searches `name` and scrolls for more result pages until a hit is the place, `pages` pages are read, every result
    is in, or 3 scrolls bring nothing. Returns (search answers, the matching hit or None, "ok" | "blocked")."""
    responses = []

    def keep(response):
        if SEARCH_API in response.url and response.request.method == "POST":
            responses.append(response)

    page.on("response", keep)
    try:
        try:
            page.goto(f"{SEARCH_URL}?search={quote(name)}", wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightError as error:
            log.warning("%r: search page did not load (%s)", name, str(error).splitlines()[0])
        answers: list[dict] = []
        read, idle, total = 0, 0, None
        deadline = time.time() + 30  # the first answer
        while True:
            new = 0
            while read < len(responses):
                response = responses[read]
                read += 1
                if response.status in (403, 429):
                    log.warning("%r: HTTP %s from Grab search", name, response.status)
                    return answers, None, "blocked"
                try:
                    body = response.json()
                    sent = json.loads(response.request.post_data or "{}")
                except (PlaywrightError, ValueError):
                    continue
                if not isinstance(body, dict) or not isinstance(sent, dict):
                    continue
                if any(a["offset"] == sent.get("offset") for a in answers):  # the page asks for offset 0 twice
                    continue
                result = body.get("searchResult") or {}
                answers.append({"offset": sent.get("offset"), "latlng": sent.get("latlng"), "payload": body})
                total = result.get("totalCount", total)
                new += 1
            hits = [
                hit for a in answers for hit in ((a["payload"].get("searchResult") or {}).get("searchMerchants") or [])
            ]
            match = pick_hit(name, hits)
            if match or len(answers) >= pages or (total is not None and len(hits) >= total):
                return answers, match, "ok"
            if not answers:
                if time.time() > deadline:
                    blocked = "request could not be satisfied" in page.content().lower()
                    log.warning("%r: no search answer%s", name, " (CloudFront)" if blocked else "")
                    return answers, None, "blocked"
                page.wait_for_timeout(500)
                continue
            idle = 0 if new else idle + 1
            if idle >= 3:
                return answers, None, "ok"
            time.sleep(random.uniform(2, 4) * pace)  # performance.md: between pages
            page.mouse.move(700, 600)  # over the results — the wheel scrolls what is under the pointer
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2500)
    finally:
        page.remove_listener("response", keep)


def search_all(context, page, run: core.Run, lines: list[str], pages: int) -> None:
    """Each place in turn, paced (performance.md); its match (or none) lands in the index as it comes."""
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)  # Grab sets its `location` cookie here
    page.wait_for_timeout(5000)
    cookie = next((c for c in context.cookies() if c["name"] == "location"), None)
    if cookie is None:
        raise Blocked("Grab set no location cookie — the search page did not load")
    pace, blocks = 1.0, 0
    for number, line in enumerate(lines):
        if number:
            time.sleep(random.uniform(6, 12) * pace)  # between searches
        name, lat, lng = parse_place(line)
        set_location(context, cookie, lat, lng)
        answers, match, outcome = search_place(page, name, pages, pace)
        if outcome == "blocked":
            blocks += 1
            log.warning("%r: blocked (%d of %d in a row)", name, blocks, MAX_BLOCKS)
            if blocks >= MAX_BLOCKS:
                raise Blocked(f"{blocks} searches in a row blocked")
            time.sleep(60)
            pace *= 2  # slow down for the rest of the run
            continue
        blocks = 0
        scraped_at = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S.000Z}"
        run.native += [
            {"label": {"place": line, "offset": a["offset"], "scrapedAt": scraped_at}, "payload": a["payload"]}
            for a in answers
        ]
        found = {
            "restaurantId": match["id"] if match else None,
            "name": (match.get("address") or {}).get("name") if match else None,
            "distanceM": round(distance_km(match) * 1000) if match else None,
        }
        run.items.append({"place": line, **found, "pagesRead": len(answers), "scrapedAt": scraped_at})
        index = core.read_done(MATCH_INDEX)
        index[line] = {**found, "at": scraped_at, "runId": run.id}
        core.write_json(MATCH_INDEX, index, overwrite=True)
        log.info(
            "%r: %s",
            name,
            f"{found['restaurantId']} {found['name']!r}, {found['distanceM']} m" if match else "no match",
        )


def write_ids() -> int:
    """data/state/grab-ids.txt: every restaurant id a search matched, one per line, for `grab menus --ids-file`."""
    ids = list(dict.fromkeys(e["restaurantId"] for e in core.read_done(MATCH_INDEX).values() if e["restaurantId"]))
    core.write_text(IDS_FILE, "".join(f"{grab_id}\n" for grab_id in ids), overwrite=True)
    return len(ids)


# ---- commands ----


def pages_arg(value: str) -> int:
    pages = int(value)
    if not 1 <= pages <= 10:
        raise argparse.ArgumentTypeError("pages must be 1-10")
    return pages


def search(args: argparse.Namespace) -> int:
    try:
        lines = list(
            dict.fromkeys(
                " ".join(line.split())
                for line in [
                    *args.place,
                    *(l for f in args.places_file for l in f.read_text(encoding="utf-8").splitlines()),
                ]
                if line.strip()
            )
        )
        for line in lines:
            parse_place(line)
    except (OSError, ValueError) as error:
        log.error("%s", error)
        return 2
    if not lines:
        log.error("no places: give --place and/or --places-file")
        return 2
    done = {} if args.refresh else core.read_done(MATCH_INDEX)
    todo = [line for line in lines if line not in done]
    log.info("%d places: %d already searched, %d to search", len(lines), len(lines) - len(todo), len(todo))
    if todo:
        if len(todo) > MAX_SEARCHES:
            log.warning("%d-search cap: %d places wait for the next run", MAX_SEARCHES, len(todo) - MAX_SEARCHES)
        run_input = {
            "places": todo[:MAX_SEARCHES],
            "pages": args.pages,
            "engine": "playwright-chromium headed",
            "session": "signed out, location cookie per place",
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=False, args=["--mute-audio"]
            )  # headless gets CloudFront's 403
            try:
                context = browser.new_context(locale="vi-VN", viewport={"width": 1366, "height": 900})
                with core.Run("grab-search", run_input) as run:
                    search_all(context, context.new_page(), run, run_input["places"], args.pages)
            except Blocked as error:
                log.error("stopped: %s — what was found is saved", error)
                status = 1
            else:
                status = 0
            finally:
                browser.close()
    else:
        status = 0
    log.info("%d matched restaurant ids → %s", write_ids(), IDS_FILE)
    return status


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
    parser = subparsers.add_parser(SOURCE, help="GrabFood restaurant ids and menus (headed browser, signed out)")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    search_parser = commands.add_parser("search", help="find places' restaurant ids into data/state/grab-ids.txt")
    search_parser.add_argument("--place", action="append", default=[], help='"name @ lat,lng" (repeatable)')
    search_parser.add_argument("--places-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    search_parser.add_argument("--pages", type=pages_arg, default=4, help="result pages per place, ~32 each (1-10)")
    search_parser.add_argument("--refresh", action="store_true", help="search places that were already searched")
    search_parser.set_defaults(run=search)
    menus_parser = commands.add_parser("menus", help="restaurant menus into data/raw/grab/<runId>.json")
    menus_parser.add_argument("--id", action="append", default=[], help="a restaurant id or link (repeatable)")
    menus_parser.add_argument("--ids-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    menus_parser.add_argument("--refresh", action="store_true", help="read restaurants that are already done again")
    menus_parser.set_defaults(run=menus)
