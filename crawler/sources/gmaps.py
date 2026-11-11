"""Google Maps place search through a headless Chromium, signed out (architecture.md, 2026-11-01 DC-05 amendment):

    python -m crawler gmaps search --query "Bún bò Huế O Xuân, Quận 3 @ 10.7843,106.6844" [--queries-file F] [--refresh]
    python -m crawler gmaps parity data/raw/gmaps/<runId>.json   compare a run with Apify's data

One page load per query: the search page carries Google's own result payload (up to 20 places) in
window.APP_INITIALIZATION_STATE. Where each field sits in a place array is ported from gosom/google-maps-scraper
(MIT, gmaps/entry.go at v1.18.1) and re-checked against Apify's items (vendor/README.md). Signed out, Google Maps
shows its "limited view": no review counts, and today's opening hours only.

Read-only: the browser only navigates (security.md → Money Safety). No Google account.
"""

import argparse
import json
import logging
import math
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from crawler import core

SOURCE = "gmaps"
ROOT = Path(__file__).resolve().parents[2]
DONE_INDEX = ROOT / "data" / "state" / "gmaps-queries.json"  # outside raw/: FoodReview reads raw/gmaps/*.json
REPORTS = ROOT / "data" / "reports"
APIFY_DIR = ROOT.parent / "FoodReview" / "data" / "raw" / "gmaps"  # read-only, for parity (security.md)
SEARCH_URL = "https://www.google.com/maps/search/"
ZOOM = 16  # what Apify's searchPageUrl used around a centre
CENTRE = re.compile(r"\s+@\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")  # FoodReview's districts.txt form
# The payload is [3][2], or [3][<key>][2] when Google sends [3] as an object under an obfuscated key ("Ag") — both
# seen in DC-05; gosom also walks every key.
PAYLOAD_JS = """() => {
  const holder = window.APP_INITIALIZATION_STATE && window.APP_INITIALIZATION_STATE[3];
  for (const value of holder ? [holder, ...Object.values(holder)] : []) {
    if (Array.isArray(value) && typeof value[2] === "string" && value[2].startsWith(")]}'")) return value[2];
  }
  return null;
}"""
FID = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+")  # Google's data id; its second half in decimal is the cid
MAX_SEARCHES = 200  # per run (performance.md)
MAX_BLOCKS = 3

log = logging.getLogger(__name__)


class Blocked(Exception):
    """Google stopped answering: the run ends, saved as FAILED with what it has (performance.md)."""


# ---- mapping: Google's place array → the compass~crawler-google-places item (docs/data/output-contract.md) ----


def at(data, *path):
    """data[i][j]… or None when any step is missing — Google's arrays are sparse (gosom's getNthElementAndCast)."""
    for index in path:
        if not isinstance(data, list) or index >= len(data):
            return None
        data = data[index]
    return data


def text(data, *path) -> str | None:
    """A string cell, or None: a cell of another type never reaches a string method and stops the run."""
    value = at(data, *path)
    return value if isinstance(value, str) else None


def number(data, *path) -> float | int | None:
    value = at(data, *path)
    return value if isinstance(value, float | int) and not isinstance(value, bool) else None


def items_of(data, *path) -> list:
    value = at(data, *path)
    return value if isinstance(value, list) else []


def places(payload: list) -> list[list]:
    """The place arrays in Google's order: a result list at [64][i][1], or — when the search went straight to one
    place — that place at [0][1][0][14]. No places: Google found nothing."""
    found = [at(entry, 1) for entry in items_of(payload, 64)] or [at(payload, 0, 1, 0, 14)]
    return [place for place in found if text(place, 11)]


def opening_hours(place: list) -> list[dict]:
    """Apify's [{day, hours}] in its wording ("06:00 to 19:00", "Đóng cửa", "Mở cửa cả ngày"). Signed out, Google
    sends today only, so this is one day (or none)."""
    return [
        {"day": day[0], "hours": ", ".join(slot[0].replace("–", " to ") for slot in items_of(day, 3) if text(slot, 0))}
        for day in items_of(place, 203, 0)
        if text(day, 0)
    ]


def to_item(place: list, query: str, page_url: str, rank: int, scraped_at: str) -> dict:
    """What Google didn't send is null, never made up (data-guideline.md)."""
    title, place_id, fid = text(place, 11), text(place, 78), text(place, 10)
    lat, lng = number(place, 9, 2), number(place, 9, 3)
    categories = [name for name in items_of(place, 13) if isinstance(name, str)]
    phone = text(place, 178, 0, 0)
    full_address = text(place, 18) or ""
    return {
        "title": title,
        "categoryName": categories[0] if categories else None,
        "categories": categories,
        "address": text(place, 39) or full_address.removeprefix(f"{title},").strip() or None,
        "location": {"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
        "price": text(place, 4, 2),
        "permanentlyClosed": at(place, 23) == 1,  # DC-05: checked against Apify's closed places
        "temporarilyClosed": at(place, 160, 0) == 1,
        "openingHours": opening_hours(place),
        "totalScore": number(place, 4, 7),
        "reviewsCount": None,  # not sent to a signed-out browser (limited view)
        "phone": phone,
        "phoneUnformatted": phone.replace(" ", "") if phone else None,
        "placeId": place_id,
        "fid": fid,
        "cid": str(int(fid.split(":0x")[1], 16)) if fid and FID.fullmatch(fid) else None,
        "kgmid": text(place, 89),
        "url": f"https://www.google.com/maps/search/?api=1&query={quote(title, safe='')}&query_place_id={place_id}"
        if place_id
        else None,
        "searchString": query,
        "searchPageUrl": page_url,
        "rank": rank,
        "language": "vi",
        "scrapedAt": scraped_at,
    }


# ---- input, resume, parity ----


def parse_query(line: str) -> tuple[str, tuple[float, float] | None]:
    """A line `name, district @ lat,lng` → (query, centre); the centre is optional. Spaces collapsed."""
    match = CENTRE.search(line)
    query = " ".join((line[: match.start()] if match else line).split())
    if not match:
        return query, None
    lat, lng = float(match[1]), float(match[2])
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"centre out of range: {line!r}")
    return query, (lat, lng)


def read_queries(queries: list[str], files: list[Path]) -> list[str]:
    """--query values, then each file's lines (UTF-8, one per line); blanks dropped, first copy kept."""
    lines = [*queries, *(line for file in files for line in file.read_text(encoding="utf-8").splitlines())]
    return list(dict.fromkeys(" ".join(line.split()) for line in lines if line.strip()))


def search_url(query: str, centre: tuple[float, float] | None) -> str:
    """Apify's searchPageUrl form. A "/" in a query (an address like 102/12) is escaped, never a path step."""
    path = SEARCH_URL + quote(query, safe=",")
    return f"{path}/@{centre[0]},{centre[1]},{ZOOM}z?hl=vi" if centre else f"{path}?hl=vi"


def distance_m(first: dict | None, second: dict | None) -> float | None:
    if not first or not second:
        return None
    lat1, lng1, lat2, lng2 = map(math.radians, (first["lat"], first["lng"], second["lat"], second["lng"]))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(haversine))


def compare(items: list[dict], apify: dict[str, list[dict]]) -> list[dict]:
    """Per query of the run: our first result vs Apify's first for the same search string, and whether Apify's first
    is anywhere in ours."""
    rows = []
    for query in dict.fromkeys(item["searchString"] for item in items):
        ours = sorted((item for item in items if item["searchString"] == query), key=lambda item: item["rank"])
        theirs = sorted(apify.get(query, []), key=lambda item: item["rank"])
        first, their_first = (ours or [None])[0], (theirs or [None])[0]
        rows.append(
            {
                "query": query,
                "crawler": len(ours),
                "apify": len(theirs),
                "sameFirst": bool(first and their_first and first["placeId"] == their_first["placeId"]),
                "apifyFirstInOurs": bool(their_first and their_first["placeId"] in {item["placeId"] for item in ours}),
                "firstApartM": round(meters)
                if (meters := distance_m(first and first["location"], their_first and their_first["location"]))
                is not None
                else None,
                "crawlerFirst": first and first["title"],
                "apifyFirst": their_first and their_first["title"],
            }
        )
    return rows


def flag_checks(items: list[dict], apify_items: list[dict]) -> dict:
    """The same place on both sides: do the closed flags and the coordinates agree?"""
    theirs = {item["placeId"]: item for item in apify_items if item.get("placeId")}
    pairs = [(item, theirs[item["placeId"]]) for item in items if item["placeId"] in theirs]
    apart = [distance_m(our["location"], their.get("location")) for our, their in pairs]
    return {
        "samePlaces": len(pairs),
        **{
            name: {
                "apifyTrue": sum(bool(their[name]) for _, their in pairs),
                "agree": sum(bool(our[name]) == bool(their[name]) for our, their in pairs),
            }
            for name in ("permanentlyClosed", "temporarilyClosed")
        },
        "titleSame": sum(our["title"] == their["title"] for our, their in pairs),
        "addressSame": sum(our["address"] == their["address"] for our, their in pairs),
        "within50m": sum(meters is not None and meters <= 50 for meters in apart),
    }


# ---- browser ----


def load_payload(page, url: str) -> list | None:
    """Google's result payload for a search page, or None when Google didn't answer: its "unusual traffic" page, a
    network error, or no payload within the timeout (one reload)."""
    for attempt in range(2):
        try:
            if attempt:
                page.reload(wait_until="domcontentloaded", timeout=45_000)
            else:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if "/sorry/" in page.url:
                log.warning("Google's unusual-traffic page")
                return None
            raw = page.wait_for_function(PAYLOAD_JS, timeout=20_000).json_value()
            return json.loads(raw.removeprefix(")]}'"))
        except (PlaywrightError, ValueError) as error:
            log.warning("no result payload (%s): %s", "reload" if attempt else "load", str(error).splitlines()[0])
    return None


def crawl(page, run: core.Run, lines: list[str]) -> None:
    """Search each query in turn, paced (performance.md); everything lands in `run` as it comes."""
    pace, blocks = 1.0, 0
    for number, line in enumerate(lines):
        if number:
            time.sleep(random.uniform(3, 8) * pace)  # between searches
        query, centre = parse_query(line)
        url = search_url(query, centre)
        payload = load_payload(page, url)
        if payload is None:
            blocks += 1
            log.warning("%r: no answer (%d of %d blocks in a row)", query, blocks, MAX_BLOCKS)
            if blocks >= MAX_BLOCKS:
                raise Blocked(f"{blocks} searches in a row got no answer")
            time.sleep(60)
            pace *= 2  # slow down for the rest of the run
            continue
        blocks = 0
        scraped_at = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S.000Z}"
        found = places(payload)
        run.native.append(
            {"label": {"searchString": query, "searchPageUrl": url, "scrapedAt": scraped_at}, "payload": payload}
        )
        run.items += [to_item(place, query, url, rank, scraped_at) for rank, place in enumerate(found, 1)]
        core.mark_done(DONE_INDEX, line, len(found), run.id)  # no places is an answer too: Google found nothing
        log.info("%r: %d places%s", query, len(found), f", first {found[0][11]!r}" if found else "")


# ---- commands ----


def search(args: argparse.Namespace) -> int:
    try:
        lines = read_queries(args.query, args.queries_file)
        for line in lines:
            parse_query(line)
    except (OSError, ValueError) as error:
        log.error("%s", error)
        return 2
    if not lines:
        log.error("no queries: give --query and/or --queries-file")
        return 2
    done = {} if args.refresh else core.read_done(DONE_INDEX)
    todo = [line for line in lines if line not in done]
    log.info("%d queries: %d already done, %d to search", len(lines), len(lines) - len(todo), len(todo))
    if not todo:
        return 0
    if len(todo) > MAX_SEARCHES:
        log.warning("%d-search cap: %d queries wait for the next run", MAX_SEARCHES, len(todo) - MAX_SEARCHES)
    # input = exactly what this run sets out to search, so a SUCCEEDED run searched every one (output-contract.md)
    run_input = {
        "queries": todo[:MAX_SEARCHES],
        "engine": "playwright-chromium headless",
        "session": "signed out",
        "language": "vi",
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(locale="vi-VN", viewport={"width": 1280, "height": 900}).new_page()
            with core.Run(SOURCE, run_input) as run:
                crawl(page, run, run_input["queries"])
        except Blocked as error:
            log.error("stopped: %s — what was collected is saved", error)
            return 1
        finally:
            browser.close()
    return 0


def parity(args: argparse.Namespace) -> int:
    run = json.loads(args.run_file.read_text(encoding="utf-8"))
    apify_items = [
        item for path in sorted(args.apify_dir.glob("*.json")) for item in json.loads(path.read_text(encoding="utf-8"))
    ]
    apify: dict[str, list[dict]] = {}
    for item in apify_items:
        if item.get("searchString") and item.get("rank") and item.get("placeId"):  # rank: a search, not a details run
            apify.setdefault(item["searchString"], []).append(item)
    rows = compare(run["items"], apify)
    compared = [row for row in rows if row["apify"]]
    flags = flag_checks(run["items"], apify_items)
    for row in compared:
        mark = "=" if row["sameFirst"] else "~" if row["apifyFirstInOurs"] else "x"
        print(f"{mark} {row['query'][:40]:<40} {row['crawlerFirst']!s:.30} | apify {row['apifyFirst']!s:.30}")
    summary = {
        "queries": len(rows),
        "comparedWithApify": len(compared),
        "sameFirst": sum(row["sameFirst"] for row in compared),
        "apifyFirstInOurs": sum(row["apifyFirstInOurs"] for row in compared),
        "noResult": {
            "crawler": sum(not row["crawler"] for row in rows),
            "apify": sum(not row["apify"] for row in rows),
        },
        "flags": flags,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    out = REPORTS / f"gmaps-parity-{run['run']['id']}.json"
    core.write_json(
        out, {"run": run["run"], "apifyDir": str(args.apify_dir), "summary": summary, "rows": rows}, overwrite=True
    )
    print(f"report: {out}")
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser(SOURCE, help="Google Maps place search (headless browser, signed out)")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    search_parser = commands.add_parser("search", help="search places into data/raw/gmaps/<runId>.json")
    search_parser.add_argument("--query", action="append", default=[], help='a query, "text [@ lat,lng]" (repeatable)')
    search_parser.add_argument("--queries-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    search_parser.add_argument("--refresh", action="store_true", help="search queries that are already done again")
    search_parser.set_defaults(run=search)
    parity_parser = commands.add_parser("parity", help="compare a run with Apify's data for the same queries")
    parity_parser.add_argument("run_file", type=Path)
    parity_parser.add_argument("--apify-dir", type=Path, default=APIFY_DIR)
    parity_parser.set_defaults(run=parity)
