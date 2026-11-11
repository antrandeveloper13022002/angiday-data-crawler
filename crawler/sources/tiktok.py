"""TikTok keyword search through a headed Chromium logged in with the owner's secondary account (architecture.md,
2026-11-01 amendment):

    python -m crawler tiktok login                        the owner logs the crawler's profile in, by hand
    python -m crawler tiktok search --keyword "…" [--keywords-file F] [--pages 2] [--refresh | --older-than 7]
    python -m crawler tiktok creators --user bachuareview [--users-file F] [--pages 1] [--older-than 6]
    python -m crawler tiktok parity data/raw/tiktok/<runId>.json   compare a run with Apify's data

Read-only: the browser only navigates and scrolls (security.md → Money Safety). A captcha waits for the owner.
"""

import argparse
import json
import logging
import random
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qsl, quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from crawler import core

SOURCE = "tiktok"
ROOT = Path(__file__).resolve().parents[2]
BROWSER_PROFILE = ROOT / "data" / "browser-profile" / SOURCE
RAW = core.DATA_RAW / SOURCE
SUBS = RAW / "subs"
DONE_INDEX = ROOT / "data" / "state" / "tiktok-keywords.json"  # outside raw/: FoodReview reads raw/tiktok/*.json
CREATORS_INDEX = ROOT / "data" / "state" / "tiktok-creators.json"
REPORTS = ROOT / "data" / "reports"
APIFY_DIR = ROOT.parent / "FoodReview" / "data" / "raw" / "tiktok"  # read-only, for parity (security.md)
SEARCH_URL = "https://www.tiktok.com/search/video?q="
SEARCH_API = "/api/search/item/full/"
SEARCH_PARAMS = {"keyword", "offset", "count", "sort_type", "publish_time", "is_filter_search", "from_page"}
PROFILE_URL = "https://www.tiktok.com/@"
PROFILE_API = "/api/post/item_list/"  # a creator's videos, newest first after the pinned ones (DC-08 spike)
USERNAME = re.compile(r"[A-Za-z0-9._]{2,24}")  # TikTok's handle rules; checked before it goes into a URL
SUBTITLE_HOSTS = (".tiktok.com", ".tiktokcdn.com", ".tiktokcdn-us.com")
MAX_SEARCH_PAGES = 200  # per run (performance.md)
MAX_BLOCKS = 3
MAX_PACE = 8  # captchas double the pauses up to this (performance.md)
CAPTCHA_WAIT_S = 300

log = logging.getLogger(__name__)


class Feed(NamedTuple):
    """A list the browser loads page by page: a keyword's search results, or a creator's videos (DC-08)."""

    api: str  # the call that brings a page
    page_key: str  # its parameter that tells pages apart
    items: str  # where a page's videos are
    more: str  # a page's "there is more" flag
    params: set[str]  # request parameters kept in the native label
    label: str  # what the target is, in labels and logs


SEARCH = Feed(SEARCH_API, "offset", "item_list", "has_more", SEARCH_PARAMS, "keyword")
PROFILE = Feed(PROFILE_API, "cursor", "itemList", "hasMore", {"count", "cursor"}, "profile")


class Blocked(Exception):
    """TikTok stopped answering: the run ends, saved as FAILED with what it has (performance.md)."""


# ---- mapping: TikTok's search item → the clockworks~tiktok-scraper item (docs/data/output-contract.md) ----


def iso(epoch: int) -> str:
    """Apify's createTimeISO format: 2026-04-12T03:25:23.000Z."""
    return f"{datetime.fromtimestamp(epoch, UTC):%Y-%m-%dT%H:%M:%S}.000Z"


def to_item(native: dict, keyword: str) -> dict:
    """What TikTok didn't send is null, never made up (data-guideline.md)."""
    author, author_stats = native.get("author") or {}, native.get("authorStats") or {}
    stats, stats_v2 = native.get("stats") or {}, native.get("statsV2") or {}
    video, music, poi = native.get("video") or {}, native.get("music") or {}, native.get("poi")
    username, video_id = author.get("uniqueId", ""), str(native["id"])
    is_slideshow = bool(native.get("imagePost"))
    created = native.get("createTime")
    return {
        "id": video_id,
        "text": native.get("desc", ""),
        "textLanguage": native.get("textLanguage"),
        "createTime": created,
        "createTimeISO": iso(int(created)) if created else None,
        "isAd": bool(native.get("isAd")),
        "authorMeta": {
            "id": author.get("id"),
            "name": username,
            "profileUrl": f"https://www.tiktok.com/@{username}",
            "nickName": author.get("nickname"),
            "verified": author.get("verified"),
            "signature": author.get("signature"),
            "avatar": author.get("avatarMedium"),
            "privateAccount": author.get("privateAccount"),
            "following": author_stats.get("followingCount"),
            "friends": author_stats.get("friendCount"),
            "fans": author_stats.get("followerCount"),
            "heart": author_stats.get("heartCount"),
            "video": author_stats.get("videoCount"),
            "digg": author_stats.get("diggCount"),
        },
        "musicMeta": {
            "musicName": music.get("title"),
            "musicAuthor": music.get("authorName"),
            "musicOriginal": music.get("original"),
            "playUrl": music.get("playUrl"),
            "coverMediumUrl": music.get("coverMedium"),
            "musicId": music.get("id"),
        },
        "locationMeta": {
            "locationName": poi.get("name"),
            "address": poi.get("address"),
            "city": poi.get("city") or None,
            "cityCode": poi.get("cityCode"),
            "countryCode": poi.get("countryCode"),
            "locationId": poi.get("id"),
        }
        if poi
        else None,
        "webVideoUrl": f"https://www.tiktok.com/@{username}/{'photo' if is_slideshow else 'video'}/{video_id}",
        "videoMeta": {
            "height": video.get("height"),
            "width": video.get("width"),
            "duration": video.get("duration"),
            "coverUrl": video.get("cover"),
            "originalCoverUrl": video.get("originCover"),
            "definition": video.get("definition"),
            "format": video.get("format"),
            "subtitleLinks": [
                {
                    "language": info.get("LanguageCodeName"),
                    "downloadLink": info.get("Url"),  # TikTok's own, expiring link — the .vtt on disk is the truth
                    "tiktokLink": info.get("Url"),
                    "source": info.get("Source"),
                    "version": info.get("Version"),
                }
                for info in video.get("subtitleInfos") or []
            ],
        },
        "diggCount": stats.get("diggCount"),
        "shareCount": stats.get("shareCount"),
        "playCount": stats.get("playCount"),
        "collectCount": stats.get("collectCount"),
        "commentCount": stats.get("commentCount"),
        "repostCount": int(stats_v2["repostCount"]) if str(stats_v2.get("repostCount", "")).isdigit() else None,
        "hashtags": [
            {"id": tag.get("id"), "name": tag.get("title"), "title": tag.get("desc") or None, "cover": None}
            for tag in native.get("challenges") or []
        ],
        "isSlideshow": is_slideshow,
        "searchQuery": keyword,
    }


def pick_subtitle(native: dict) -> dict | None:
    """Vietnamese first, else the first — the one FoodReview reads (its pickSubtitle)."""
    infos = (native.get("video") or {}).get("subtitleInfos") or []
    vietnamese = [info for info in infos if str(info.get("LanguageCodeName", "")).startswith("vie")]
    return (vietnamese or infos or [None])[0]


def is_tiktok_https(url: str) -> bool:
    """Follow-up downloads only from TikTok's own hosts, over https (security.md)."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(SUBTITLE_HOSTS)


# ---- input, resume, parity ----


def read_keywords(keywords: list[str], files: list[Path]) -> list[str]:
    """--keyword values, then each file's lines (UTF-8, one keyword per line); spaces collapsed, blanks dropped,
    first copy kept."""
    lines = [*keywords, *(line for file in files for line in file.read_text(encoding="utf-8").splitlines())]
    return list(dict.fromkeys(" ".join(line.split()) for line in lines if line.strip()))


def read_done() -> dict:
    return core.read_done(DONE_INDEX)


def mark_done(keyword: str, results: int, run_id: str) -> None:
    """Only a keyword that found videos is done; one that found none is tried again next run (as FoodReview did)."""
    core.mark_done(DONE_INDEX, keyword, results, run_id)


def fill(items) -> dict:
    items = list(items)
    return {
        "videos": len(items),
        "subtitles": sum(bool((item.get("videoMeta") or {}).get("subtitleLinks")) for item in items),
        "location": sum(bool(item.get("locationMeta")) for item in items),
        "views": sum(bool(item.get("playCount")) for item in items),
    }


def compare(items: list[dict], apify: dict[str, dict[str, dict]]) -> list[dict]:
    """Per keyword of the run: our videos vs Apify's for the same keyword — overlap and how often fields are filled.
    Both sides are in the same Apify shape, so the same counts apply."""
    rows = []
    for keyword in dict.fromkeys(item["searchQuery"] for item in items):
        ours = {item["id"]: item for item in items if item["searchQuery"] == keyword}
        theirs = apify.get(keyword, {})
        rows.append(
            {
                "keyword": keyword,
                "both": len(ours.keys() & theirs.keys()),
                "crawler": fill(ours.values()),
                "apify": fill(theirs.values()),
            }
        )
    return rows


# ---- browser ----


def open_profile(playwright, *, headless: bool = False):
    """The crawler's own profile (never the owner's browser), headed — headless runs got a captcha on every keyword
    (DC-01)."""
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(BROWSER_PROFILE),
        channel="chromium",
        headless=headless,
        locale="vi-VN",
        viewport={"width": 1366, "height": 900},
        args=["--mute-audio"],
    )


def is_logged_in(context) -> bool:
    """By the session cookie's name only — its value is never read out (security.md)."""
    return any(cookie["name"] == "sessionid" for cookie in context.cookies("https://www.tiktok.com"))


def captcha_box(page):
    return page.locator("[id*='captcha' i], [class*='captcha' i]").or_(page.get_by_text("ghép hình"))


def wait_out_captcha(page) -> bool:
    """Waits for the owner to solve a visible captcha; nothing here solves it. True if one was there and is gone."""
    captcha = captcha_box(page)
    if not captcha.first.is_visible():
        return False
    log.warning("captcha — solve it in the browser window (waiting up to %d min)", CAPTCHA_WAIT_S // 60)
    deadline = time.time() + CAPTCHA_WAIT_S
    while time.time() < deadline and captcha.first.is_visible():
        page.wait_for_timeout(2000)
    return not captcha.first.is_visible()


def target_url(feed: Feed, target: str) -> str:
    """A keyword's search page, or a creator's profile page (the handle was checked against USERNAME)."""
    return SEARCH_URL + quote(target) if feed is SEARCH else PROFILE_URL + target


def collect_pages(page, feed: Feed, target: str, pages: int, pace: float) -> tuple[list[dict], bool]:
    """Every list page the browser loads for `target`, by the feed's page key. One scroll can load several pages, so
    every response is kept, not one per scroll (DC-01). Stops at `pages` pages, the last page, or 3 idle scrolls.
    Also says whether TikTok showed a captcha (solved or not)."""
    responses = []

    def keep(response):
        if feed.api in response.url:
            responses.append(response)

    page.on("response", keep)
    try:
        page.goto(target_url(feed, target), wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        challenged = captcha_box(page).first.is_visible()
        if challenged and wait_out_captcha(page):  # the page asked before the captcha and won't ask again by itself
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        found: dict[str, dict] = {}
        read, idle, has_more = 0, 0, True
        while True:
            new = 0
            while read < len(responses):
                response = responses[read]
                read += 1
                try:
                    payload = json.loads(response.body())
                except (PlaywrightError, ValueError):
                    log.warning("%r: empty list response (HTTP %s)", target, response.status)
                    continue
                params = {k: v for k, v in parse_qsl(urlsplit(response.request.url).query) if k in feed.params}
                key = params.get(feed.page_key, "?")
                if key not in found:
                    found[key] = {"page": int(key) if key.isdigit() else key, "params": params, "payload": payload}
                    has_more = bool(payload.get(feed.more))
                    new += 1
            idle = 0 if new else idle + 1
            if len(found) >= pages or not has_more or idle >= 3:
                return list(found.values()), challenged  # pages in the order the browser asked for them
            time.sleep(random.uniform(2, 5) * pace)  # performance.md: between pages
            page.mouse.move(700, 600)  # over the results grid — the wheel scrolls what is under the pointer
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(2500)
    finally:
        page.remove_listener("response", keep)


def save_subtitle(context, native: dict) -> bool:
    """The picked .vtt to data/raw/tiktok/subs/<id>.vtt, once per video ever (BR-RAW-01). True if newly saved."""
    info = pick_subtitle(native)
    if not info:
        return False
    try:
        path = core.id_path(SUBS, str(native.get("id")), ".vtt")
    except ValueError:
        log.warning("subtitle skipped: odd video id %r", native.get("id"))
        return False
    url = str(info.get("Url", ""))
    if path.exists():
        return False
    if not is_tiktok_https(url):
        log.warning("subtitle for %s skipped: not a TikTok https link", native["id"])
        return False
    time.sleep(random.uniform(0.5, 1.5))  # performance.md
    response = context.request.get(url, timeout=30_000)
    text = response.text() if response.ok else ""
    if not text.lstrip("﻿").startswith("WEBVTT"):
        log.warning("subtitle for %s: HTTP %s, not a WebVTT file", native["id"], response.status)
        return False
    core.write_text(path, text)
    return True


# ---- commands ----


def login(args: argparse.Namespace) -> int:
    """Opens Playwright's Chromium as a normal, non-automated window on the crawler's profile at TikTok's login page;
    the owner logs in with the SECONDARY account by hand (BR-ACC-01) and closes it. Nothing here types or clicks."""
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    log.info("log in with the SECONDARY TikTok account (email or username + password), then close the window")
    subprocess.run(
        [
            executable,
            f"--user-data-dir={BROWSER_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.tiktok.com/login",
        ],
        check=False,
    )
    with sync_playwright() as playwright:
        context = open_profile(playwright, headless=True)
        logged_in = is_logged_in(context)
        context.close()
    if logged_in:
        log.info("logged in — the session is kept in %s", BROWSER_PROFILE)
        return 0
    log.error("no TikTok session found — run login again and log in before closing the window")
    return 1


def items_of(feed: Feed, target: str, natives: list[dict]) -> list[dict]:
    """Contract items: a search result carries its keyword; a creator's video has no keyword and names the profile
    it came from (`profile`)."""
    if feed is SEARCH:
        return [to_item(native, target) for native in natives]
    return [to_item(native, None) | {"profile": target} for native in natives]


def crawl(context, run: core.Run, targets: list[str], pages: int, feed: Feed = SEARCH) -> None:
    """Each keyword (or creator) in turn, paced and capped (performance.md); everything lands in `run` as it comes."""
    page = context.pages[0] if context.pages else context.new_page()
    pace, blocks, used = 1.0, 0, 0
    for number, target in enumerate(targets):
        if used + pages > MAX_SEARCH_PAGES:
            left = len(targets) - number
            log.warning("%d-page cap reached: %d %ss wait for the next run", MAX_SEARCH_PAGES, left, feed.label)
            return
        if number:
            time.sleep(random.uniform(5, 15) * pace)  # between keywords / creators
        found, challenged = collect_pages(page, feed, target, pages, pace)
        if challenged and pace < MAX_PACE:  # TikTok thinks we go too fast: slower for the rest of the run
            pace = min(pace * 2, MAX_PACE)
            log.warning("captcha: pauses ×%g for the rest of the run", pace)
        used += max(len(found), 1)
        natives = [native for entry in found for native in entry["payload"].get(feed.items) or []]
        if not natives:
            blocks += 1
            log.warning("%r: no videos (%d of %d blocks in a row)", target, blocks, MAX_BLOCKS)
            if blocks >= MAX_BLOCKS:
                raise Blocked(f"{blocks} {feed.label}s in a row got no videos")
            time.sleep(60)
            pace *= 2  # slow down for the rest of the run
            continue
        blocks = 0
        scraped_at = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"
        run.native += [
            {
                "label": {
                    feed.label: target,
                    feed.page_key: entry["page"],
                    "params": entry["params"],
                    "scrapedAt": scraped_at,
                },
                "payload": entry["payload"],
            }
            for entry in found
        ]
        run.items += items_of(feed, target, natives)
        subtitles = sum(save_subtitle(context, native) for native in natives)
        core.mark_done(DONE_INDEX if feed is SEARCH else CREATORS_INDEX, target, len(natives), run.id)
        log.info("%r: %d pages, %d videos, %d new subtitles", target, len(found), len(natives), subtitles)


def search(args: argparse.Namespace) -> int:
    keywords = read_keywords(args.keyword, args.keywords_file)
    if not keywords:
        log.error("no keywords: give --keyword and/or --keywords-file")
        return 2
    done = {} if args.refresh else read_done()
    todo = core.due(keywords, done, args.older_than)
    log.info(
        "%d keywords: %d already done, %d to search, %d pages each",
        len(keywords),
        len(keywords) - len(todo),
        len(todo),
        args.pages,
    )
    if not todo:
        return 0
    run_input = {"keywords": todo, "pages": args.pages, "engine": "playwright-chromium", "session": "secondary account"}
    return browse(SEARCH, todo, args.pages, run_input)


def read_creators(users: list[str], files: list[Path]) -> list[str]:
    """--user values, then each file's lines (UTF-8, one handle per line, a leading @ dropped); first copy kept. A
    handle TikTok wouldn't allow is refused: it goes into a URL (security.md)."""
    handles = [handle.removeprefix("@") for handle in read_keywords(users, files)]
    for handle in handles:
        if not USERNAME.fullmatch(handle):
            raise ValueError(f"not a TikTok handle: {handle!r}")
    return list(dict.fromkeys(handles))


def creators(args: argparse.Namespace) -> int:
    """The newest videos of each creator, from their profile page (DC-08, owner 2026-11-02)."""
    try:
        users = read_creators(args.user, args.users_file)
    except (OSError, ValueError) as error:
        log.error("%s", error)
        return 2
    if not users:
        log.error("no creators: give --user and/or --users-file")
        return 2
    done = {} if args.refresh else core.read_done(CREATORS_INDEX)
    todo = core.due(users, done, args.older_than)
    log.info(
        "%d creators: %d already done, %d to read, %d pages each",
        len(users),
        len(users) - len(todo),
        len(todo),
        args.pages,
    )
    if not todo:
        return 0
    run_input = {"profiles": todo, "pages": args.pages, "engine": "playwright-chromium", "session": "secondary account"}
    return browse(PROFILE, todo, args.pages, run_input)


def browse(feed: Feed, todo: list[str], pages: int, run_input: dict) -> int:
    """One run over `todo` in the logged-in crawler profile; a block ends it with what was collected saved."""
    with sync_playwright() as playwright:
        context = open_profile(playwright)
        try:
            if not is_logged_in(context):  # before the run starts, so no empty run file is written
                log.error("no TikTok session in the crawler profile — run: python -m crawler tiktok login")
                return 1
            with core.Run(SOURCE, run_input) as run:
                crawl(context, run, todo, pages, feed)
        except Blocked as error:
            log.error("stopped: %s — what was collected is saved", error)
            return 1
        finally:
            context.close()
    return 0


def parity(args: argparse.Namespace) -> int:
    run = json.loads(args.run_file.read_text(encoding="utf-8"))
    apify: dict[str, dict[str, dict]] = {}
    for path in sorted(args.apify_dir.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            if item.get("searchQuery") and item.get("id"):
                apify.setdefault(item["searchQuery"], {})[item["id"]] = item
    rows = compare(run["items"], apify)
    print(f"{'keyword':<34} {'crawler':>7} {'apify':>5} {'both':>4}   subtitles c/a   location c/a   views c/a")
    for row in rows:
        ours, theirs = row["crawler"], row["apify"]
        print(
            f"{row['keyword'][:34]:<34} {ours['videos']:>7} {theirs['videos']:>5} {row['both']:>4}   "
            f"{ours['subtitles']:>5}/{theirs['subtitles']:<5}    {ours['location']:>5}/{theirs['location']:<5}   "
            f"{ours['views']:>4}/{theirs['views']}"
        )
    out = REPORTS / f"tiktok-parity-{run['run']['id']}.json"
    core.write_json(out, {"run": run["run"], "apifyDir": str(args.apify_dir), "rows": rows}, overwrite=True)
    print(f"report: {out}")
    return 0


def days_arg(value: str) -> float:
    days = float(value)
    if not 0 < days <= 365:
        raise argparse.ArgumentTypeError("days must be more than 0 and at most 365")
    return days


def pages_arg(value: str) -> int:
    pages = int(value)
    if not 1 <= pages <= 10:
        raise argparse.ArgumentTypeError("pages must be 1-10")
    return pages


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        SOURCE, help="TikTok keyword search and creators (headed browser, secondary account)"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    commands.add_parser("login", help="log the crawler's browser profile in with the secondary account").set_defaults(
        run=login
    )
    search_parser = commands.add_parser("search", help="search keywords into data/raw/tiktok/<runId>.json")
    search_parser.add_argument("--keyword", action="append", default=[], help="a keyword (repeatable)")
    search_parser.add_argument("--keywords-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    search_parser.add_argument(
        "--pages", type=pages_arg, default=2, help="search pages per keyword, 12 videos each (1-10)"
    )
    search_parser.add_argument("--refresh", action="store_true", help="search keywords that are already done again")
    search_parser.add_argument(
        "--older-than",
        type=days_arg,
        metavar="DAYS",
        help="search again keywords last done more than DAYS ago (weekly: 7)",
    )
    search_parser.set_defaults(run=search)
    creators_parser = commands.add_parser(
        "creators", help="the newest videos of creators, into data/raw/tiktok/<runId>.json"
    )
    creators_parser.add_argument("--user", action="append", default=[], help="a TikTok handle (repeatable)")
    creators_parser.add_argument("--users-file", action="append", type=Path, default=[], help="UTF-8, one per line")
    creators_parser.add_argument(
        "--pages", type=pages_arg, default=1, help="profile pages per creator, ~16 videos each"
    )
    creators_parser.add_argument("--refresh", action="store_true", help="read creators that are already done again")
    creators_parser.add_argument(
        "--older-than",
        type=days_arg,
        metavar="DAYS",
        help="read again creators last done more than DAYS ago (weekly: 7)",
    )
    creators_parser.set_defaults(run=creators)
    parity_parser = commands.add_parser("parity", help="compare a run with Apify's data for the same keywords")
    parity_parser.add_argument("run_file", type=Path)
    parity_parser.add_argument("--apify-dir", type=Path, default=APIFY_DIR)
    parity_parser.set_defaults(run=parity)
