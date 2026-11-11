import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from crawler.__main__ import main
from crawler.sources import gmaps

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gmaps" / "search_payloads.json"
if not FIXTURE_PATH.exists():  # scraped data is git-ignored (public repo); the owner's machine has it
    pytest.skip("tests/fixtures/gmaps not present", allow_module_level=True)
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
LIST, SINGLE = FIXTURE["list"], FIXTURE["single"]  # real payloads from the DC-05 spikes, trimmed
AT = "2026-11-01T07:00:00.000Z"


def items(fixture: dict) -> list[dict]:
    url = gmaps.search_url(fixture["query"], None)
    return [
        gmaps.to_item(place, fixture["query"], url, rank, AT)
        for rank, place in enumerate(gmaps.places(fixture["payload"]), 1)
    ]


def test_every_item_carries_what_foodreview_reads():
    """FoodReview's GooglePlace (scripts/pipeline/review.ts) and its toPlace filter: title and placeId required."""
    for item in items(LIST) + items(SINGLE):
        assert isinstance(item["title"], str) and item["title"]
        assert item["placeId"].startswith("ChIJ")
        assert isinstance(item["address"], str)
        assert isinstance(item["location"]["lat"], float) and isinstance(item["location"]["lng"], float)
        assert isinstance(item["categoryName"], str)
        assert item["price"] is None or isinstance(item["price"], str)
        assert isinstance(item["permanentlyClosed"], bool) and isinstance(item["temporarilyClosed"], bool)
        assert all(set(hours) == {"day", "hours"} for hours in item["openingHours"])
        assert item["url"].startswith("https://www.google.com/maps/search/?api=1&query=")
        assert isinstance(item["rank"], int) and item["searchString"]


def test_a_result_list_maps_to_apify_values_in_googles_order():
    found = items(LIST)
    assert [item["rank"] for item in found] == [1, 2, 3, 4]
    first = found[0]  # Apify saw the same place for this search (FoodReview data/raw/gmaps)
    assert first["title"] == "Quán nem nướng Anh Thành"
    assert first["address"] == "67 Võ Văn Kiệt, Cầu Ông Lãnh, Hồ Chí Minh, Việt Nam"
    assert first["placeId"] == "ChIJiy9J3xQvdTERRV9CXNqVCvw"
    assert first["fid"] == "0x31752f14df492f8b:0xfc0a95da5c425f45" and first["cid"] == "18161493212408209221"
    assert first["kgmid"] == "/g/11f4qx_862"
    assert first["phone"] == "+84 901 466 716" and first["phoneUnformatted"] == "+84901466716"
    assert first["location"] == {"lat": 10.7647216, "lng": 106.6986762}
    assert first["categoryName"] == "Quán ăn nhỏ" and first["totalScore"] == 4.3
    assert first["openingHours"] == [{"day": "Chủ Nhật", "hours": "06:00 to 19:00"}]  # today only (limited view)
    assert first["reviewsCount"] is None
    assert first["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Qu%C3%A1n%20nem%20n%C6%B0%E1%BB%9Bng%20Anh%20Th%C3%A0nh"
        "&query_place_id=ChIJiy9J3xQvdTERRV9CXNqVCvw"
    )


def test_a_search_that_went_straight_to_one_place():
    found = items(SINGLE)
    assert len(found) == 1
    assert found[0]["placeId"] == "ChIJBxJudLgvdTERRYD9jSs8atE"
    assert found[0]["openingHours"] == [{"day": "Chủ Nhật", "hours": "10:00 to 22:00"}]


@pytest.mark.parametrize(
    ("group", "permanently", "temporarily"),
    [("permanentlyClosed", True, False), ("temporarilyClosed", False, True), ("open", False, False)],
)
def test_closed_flags_match_what_apify_said(group, permanently, temporarily):
    for place in FIXTURE[group]:
        item = gmaps.to_item(place, "q", "u", 1, AT)
        assert (item["permanentlyClosed"], item["temporarilyClosed"]) == (permanently, temporarily), item["title"]


def test_no_places_and_sparse_places():
    assert gmaps.places([]) == []
    assert gmaps.places([None] * 70) == []  # Google answered, found nothing
    assert gmaps.places([None] * 64 + [[[None, [None] * 5]]]) == []  # no title: not a place
    sparse = [None] * 11 + ["Quán không tên"]
    item = gmaps.to_item(sparse, "q", "u", 1, AT)
    assert item["title"] == "Quán không tên"
    assert item["location"] is item["placeId"] is item["url"] is item["cid"] is item["address"] is None
    assert item["openingHours"] == [] and item["categories"] == []
    assert item["permanentlyClosed"] is False


def test_cells_of_the_wrong_type_become_null_not_a_crash():
    """Google changes its layout now and then; one odd cell must not stop a run of 200 searches."""
    odd = [None] * 4 + [[None] * 2 + [5, None, None, None, None, "4,5"]] + [None] * 4 + [[None, None, "10.7", True]]
    odd += [12345, "Quán lạ", None, "Phở"] + [None] * 164 + [[[7]]] + [None] * 24 + [[[["Thứ Hai", 1, None, 5]]]]
    odd[78] = ["ChIJ"]
    item = gmaps.to_item(odd, "q", "u", 1, AT)
    assert item["title"] == "Quán lạ"
    assert item["location"] is item["price"] is item["totalScore"] is item["fid"] is item["cid"] is None
    assert item["phone"] is item["placeId"] is item["url"] is None and item["categories"] == []
    assert item["openingHours"] == [{"day": "Thứ Hai", "hours": ""}]
    assert gmaps.places([None] * 64 + ["not a list"]) == []
    assert gmaps.to_item([None] * 10 + ["0x1:0xzz", "Quán"], "q", "u", 1, AT)["cid"] is None


def test_hours_in_apifys_wording():
    place = [None] * 203 + [
        [[["Thứ Hai", 1, None, [["06:00–10:00"], ["16:00–21:30"]]], ["Thứ Ba", 2, None, [["Đóng cửa"]]]]]
    ]
    assert gmaps.opening_hours(place) == [
        {"day": "Thứ Hai", "hours": "06:00 to 10:00, 16:00 to 21:30"},
        {"day": "Thứ Ba", "hours": "Đóng cửa"},
    ]


def test_query_lines_with_and_without_a_centre():
    assert gmaps.parse_query("Bún bò O Xuân,  Quận 3 @ 10.7843,106.6844") == (
        "Bún bò O Xuân, Quận 3",
        (10.7843, 106.6844),
    )
    assert gmaps.parse_query("Phở Hoà, Quận 3") == ("Phở Hoà, Quận 3", None)
    assert gmaps.parse_query("Quán @ góc phố") == ("Quán @ góc phố", None)  # not a centre: part of the name
    with pytest.raises(ValueError):
        gmaps.parse_query("Phở @ 91.0,106.0")
    assert gmaps.search_url("Phở Hoà, Quận 3", (10.7843, 106.6844)) == (
        "https://www.google.com/maps/search/Ph%E1%BB%9F%20Ho%C3%A0,%20Qu%E1%BA%ADn%203/@10.7843,106.6844,16z?hl=vi"
    )


def test_a_query_never_leaves_the_search_path():
    url = gmaps.search_url("102/12 Cống Quỳnh/../../x?y=1#z", None)
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.query, parts.fragment) == ("https", "www.google.com", "hl=vi", "")
    assert parts.path.count("/") == 3  # /maps/search/<query>


def test_queries_are_deduped(tmp_path):
    file = tmp_path / "q.txt"
    file.write_text("Phở Hoà, Quận 3\n\n  Phở  Hoà, Quận 3 \nBánh mì Huỳnh Hoa @ 10.77,106.69\n", encoding="utf-8")
    assert gmaps.read_queries(["Phở Hoà, Quận 3"], [file]) == ["Phở Hoà, Quận 3", "Bánh mì Huỳnh Hoa @ 10.77,106.69"]


def test_search_refuses_bad_input_before_any_browser(tmp_path, caplog):
    assert main(["gmaps", "search"]) == 2
    assert main(["gmaps", "search", "--query", "Phở @ 10.7,200.5"]) == 2
    assert main(["gmaps", "search", "--queries-file", str(tmp_path / "missing.txt")]) == 2


def test_parity_compares_first_results_and_flags():
    ours = items(LIST)
    apify_first = {**ours[1], "rank": 1}  # Apify ranked our second result first
    rows = gmaps.compare(ours, {LIST["query"]: [apify_first]})
    assert rows[0] | {"firstApartM": None} == {
        "query": LIST["query"],
        "crawler": 4,
        "apify": 1,
        "sameFirst": False,
        "apifyFirstInOurs": True,
        "firstApartM": None,
        "crawlerFirst": "Quán nem nướng Anh Thành",
        "apifyFirst": ours[1]["title"],
    }
    assert rows[0]["firstApartM"] > 0
    flags = gmaps.flag_checks(ours, [{**ours[0], "permanentlyClosed": True}])
    assert flags["samePlaces"] == 1 and flags["permanentlyClosed"] == {"apifyTrue": 1, "agree": 0}
    assert flags["within50m"] == 1 and flags["titleSame"] == 1
