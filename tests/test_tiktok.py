import argparse
import json
import re
from pathlib import Path

import pytest

from crawler.__main__ import main
from crawler.sources import tiktok

FIXTURE = Path(__file__).parent / "fixtures" / "tiktok" / "search_items.json"
if not FIXTURE.exists():  # scraped data is git-ignored (public repo); the owner's machine has it
    pytest.skip("tests/fixtures/tiktok not present", allow_module_level=True)
NATIVE = json.loads(FIXTURE.read_text(encoding="utf-8"))  # real search items from DC-01, trimmed
FULL, PHOTO, BARE = NATIVE  # subtitles + location; photo post; no location
FOODREVIEW_URL = re.compile(r"^https://www\.tiktok\.com/@[\w.]+/(video|photo)/\d+$")  # FoodReview's toClip filter


def test_every_item_carries_what_foodreview_reads():
    for native in NATIVE:
        item = tiktok.to_item(native, "review quán ăn nha trang")
        assert isinstance(item["id"], str) and item["id"].isdigit()
        assert FOODREVIEW_URL.match(item["webVideoUrl"])
        assert isinstance(item["text"], str) and item["text"]
        assert item["authorMeta"]["name"]
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z", item["createTimeISO"])
        assert isinstance(item["playCount"], int) and isinstance(item["diggCount"], int)
        assert isinstance(item["videoMeta"]["duration"], int)
        assert isinstance(item["isSlideshow"], bool) and isinstance(item["isAd"], bool)
        assert isinstance(item["videoMeta"]["subtitleLinks"], list)
        assert item["searchQuery"] == "review quán ăn nha trang"


def test_maps_a_video_with_subtitles_and_a_location():
    item = tiktok.to_item(FULL, "review quán ăn nha trang")
    assert item["id"] == "7648623766980840725"
    assert item["webVideoUrl"] == "https://www.tiktok.com/@hoaoididau/video/7648623766980840725"
    assert item["createTimeISO"] == "2026-06-07T12:06:41.000Z"
    assert item["playCount"] == FULL["stats"]["playCount"] and item["diggCount"] == FULL["stats"]["diggCount"]
    assert item["locationMeta"]["locationName"] == "Nha Trang"
    assert item["locationMeta"]["address"] == FULL["poi"]["address"]
    assert item["videoMeta"]["subtitleLinks"][0]["language"] == "vie-VN"
    assert item["videoMeta"]["subtitleLinks"][0]["downloadLink"] == FULL["video"]["subtitleInfos"][0]["Url"]
    assert [tag["name"] for tag in item["hashtags"]] == [tag["title"] for tag in FULL["challenges"]]
    assert item["isSlideshow"] is False


def test_photo_post_is_a_slideshow_with_a_photo_link():
    item = tiktok.to_item(PHOTO, "k")
    assert item["isSlideshow"] is True
    assert item["webVideoUrl"] == "https://www.tiktok.com/@tuyetthanh.2011/photo/7685988978910072084"


def test_what_tiktok_did_not_send_stays_null():
    assert tiktok.to_item(BARE, "k")["locationMeta"] is None
    native = {key: value for key, value in FULL.items() if key not in ("stats", "statsV2")}
    item = tiktok.to_item(native, "k")
    assert item["playCount"] is None and item["diggCount"] is None and item["repostCount"] is None


def test_vietnamese_subtitle_first_else_the_first():
    english = {"LanguageCodeName": "eng-US", "Url": "https://a.tiktok.com/e"}
    vietnamese = {"LanguageCodeName": "vie-VN", "Url": "https://a.tiktok.com/v"}
    assert tiktok.pick_subtitle({"video": {"subtitleInfos": [english, vietnamese]}}) == vietnamese
    assert tiktok.pick_subtitle({"video": {"subtitleInfos": [english]}}) == english
    assert tiktok.pick_subtitle({"video": {}}) is None
    assert tiktok.pick_subtitle({}) is None


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://v16-webapp.tiktok.com/abc/?a=1988", True),
        ("https://v16m-webapp.tiktokcdn-us.com/x", True),
        ("https://p16-common-sign.tiktokcdn.com/x", True),
        ("http://v16-webapp.tiktok.com/x", False),
        ("https://evil.com/x", False),
        ("https://tiktok.com.evil.com/x", False),
        ("https://eviltiktok.com/x", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_subtitles_come_only_from_tiktok_over_https(url, allowed):
    assert tiktok.is_tiktok_https(url) is allowed


def test_keywords_from_args_and_files_trimmed_and_unique(tmp_path):
    keywords_file = tmp_path / "keywords.txt"
    keywords_file.write_text("ăn gì  đà lạt\n\n  quán ngon nha trang \nreview quán ăn nha trang\n", encoding="utf-8")
    assert tiktok.read_keywords(["review quán ăn nha trang", " "], [keywords_file]) == [
        "review quán ăn nha trang",
        "ăn gì đà lạt",
        "quán ngon nha trang",
    ]


@pytest.mark.parametrize("value", ["0", "11", "-1"])
def test_page_count_is_capped(value):
    with pytest.raises(argparse.ArgumentTypeError):
        tiktok.pages_arg(value)


def test_done_keywords_are_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok, "DONE_INDEX", tmp_path / "state" / "done.json")
    assert tiktok.read_done() == {}
    tiktok.mark_done("ăn gì đà lạt", 24, "run-1")
    tiktok.mark_done("quán ngon nha trang", 12, "run-1")
    done = tiktok.read_done()
    assert set(done) == {"ăn gì đà lạt", "quán ngon nha trang"}
    assert done["ăn gì đà lạt"]["results"] == 24 and done["ăn gì đà lạt"]["runId"] == "run-1"


def test_compare_counts_overlap_and_fill():
    ours = [tiktok.to_item(FULL, "k"), tiktok.to_item(BARE, "k")]
    apify = {
        "k": {FULL["id"]: {"id": FULL["id"], "playCount": 5, "locationMeta": None, "videoMeta": {"subtitleLinks": []}}}
    }
    [row] = tiktok.compare(ours, apify)
    assert row["both"] == 1
    assert row["crawler"] == {"videos": 2, "subtitles": 2, "location": 1, "views": 2}
    assert row["apify"] == {"videos": 1, "subtitles": 0, "location": 0, "views": 1}


def test_search_without_keywords_is_refused_before_any_browser():
    assert main(["tiktok", "search"]) == 2


def test_fixture_holds_no_session_tokens():
    assert not re.search(r"msToken|sessionid|ttwid|sid_tt", FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("days", ["0", "-1", "366", "soon"])
def test_older_than_refuses_odd_day_counts(days):
    with pytest.raises(SystemExit):
        main(["tiktok", "search", "--keyword", "ăn gì quận 1", "--older-than", days])


# ---- creators (DC-08) ----


def test_creator_handles_are_checked_before_they_reach_a_url(tmp_path):
    file = tmp_path / "creators.txt"
    file.write_text("@bachuareview\n\nwil.eats.danang\n_.ngoctai._\nbachuareview\n", encoding="utf-8")
    assert tiktok.read_creators(["tieusanhanuong"], [file]) == [
        "tieusanhanuong",
        "bachuareview",
        "wil.eats.danang",
        "_.ngoctai._",
    ]
    for bad in ["../../x", "a/b", "evil.com?x=1", "x", "a" * 25, "tên có dấu", "a#b"]:
        with pytest.raises(ValueError):
            tiktok.read_creators([bad], [])


def test_a_creators_video_names_its_profile_and_has_no_keyword():
    [item] = tiktok.items_of(tiktok.PROFILE, "hoaoididau", [FULL])
    assert item["profile"] == "hoaoididau" and item["searchQuery"] is None
    assert item["id"] == "7648623766980840725" and FOODREVIEW_URL.match(item["webVideoUrl"])
    [searched] = tiktok.items_of(tiktok.SEARCH, "review quán ăn nha trang", [FULL])
    assert searched["searchQuery"] == "review quán ăn nha trang" and "profile" not in searched


def test_feed_urls():
    assert tiktok.target_url(tiktok.PROFILE, "bachuareview") == "https://www.tiktok.com/@bachuareview"
    assert (
        tiktok.target_url(tiktok.SEARCH, "ăn gì quận 1")
        == "https://www.tiktok.com/search/video?q=%C4%83n%20g%C3%AC%20qu%E1%BA%ADn%201"
    )


def test_creators_refuses_bad_input_before_any_browser(tmp_path):
    assert main(["tiktok", "creators"]) == 2
    assert main(["tiktok", "creators", "--user", "../../etc"]) == 2
    assert main(["tiktok", "creators", "--users-file", str(tmp_path / "missing.txt")]) == 2


def test_the_browser_profile_is_a_folder_not_a_feed():
    # Regression (2026-11-09): DC-08's `PROFILE = Feed(...)` shadowed the profile folder, so every run died in
    # open_profile with "'Feed' object has no attribute 'mkdir'" before a browser opened.
    assert isinstance(tiktok.BROWSER_PROFILE, Path)
    assert tiktok.BROWSER_PROFILE.parts[-2:] == ("browser-profile", "tiktok")
    assert isinstance(tiktok.PROFILE, tiktok.Feed)
