import json
from pathlib import Path

import pytest

from crawler.__main__ import main
from crawler.sources import grab

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "grab" / "merchants.json"
if not FIXTURE_PATH.exists():  # scraped data is git-ignored (public repo); the owner's machine has it
    pytest.skip("tests/fixtures/grab not present", allow_module_level=True)
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))  # real payloads from the DC-09 spike
AT = "2026-11-11T12:51:27.000Z"
FULL = grab.to_item(FIXTURE["full"]["merchant"], grab.PAGE_URL + "5-CYVVG74JRBM1LN", AT)
TRIMMED = grab.to_item(FIXTURE["trimmed"]["merchant"], grab.PAGE_URL + "5-C8JUMEU1KBB3WE", AT)


def test_item_has_every_field_of_the_apify_actor():
    assert FULL.keys() == FIXTURE["apifyFull"].keys() | {"menu"}


def test_restaurant_fields_match_apify_for_the_same_restaurant():
    apify = FIXTURE["apifyFull"]
    for field in (
        "restaurantId",
        "name",
        "chainName",
        "address",
        "city",
        "postcode",
        "imageUrl",
        "currency",
        "openingHours",
        "menuItemCount",
    ):
        assert FULL[field] == apify[field], field
    assert FULL["country"] == "vn" and FULL["scrapedAt"] == AT
    assert FULL["menuItemCount"] == len(FULL["menu"]) == 25


def test_menu_item_in_apify_units_and_shape():
    first = FULL["menu"][0]
    assert first == {
        "category": "Menu",
        "name": "ốp La thịt bò",
        "description": None,
        "price": 400,  # 40.000đ, as the Apify actor writes it
        "discountedPrice": None,
        "discountPct": None,
        "available": True,
        "imageUrl": None,
    }


def test_discount_and_missing_available():
    discounted, unlisted, plain = TRIMMED["menu"]
    assert (discounted["price"], discounted["discountedPrice"], discounted["discountPct"]) == (1400, 700, 50)
    assert unlisted["available"] is None  # Grab left it out: null, not guessed
    assert plain["available"] is True and plain["discountedPrice"] is None


def test_what_grab_did_not_send_is_null():
    bare = grab.to_item({"ID": "5-C75BL6WCMANWLT"}, grab.PAGE_URL + "5-C75BL6WCMANWLT", AT)
    assert bare["menu"] == [] and bare["menuItemCount"] == 0
    assert bare["name"] is bare["cuisine"] is bare["rating"] is bare["address"] is bare["currency"] is None


@pytest.mark.parametrize(
    ("minor", "price"), [(40000, 400), (12345, 123.45), (0, 0), (None, None), ("40000", None), (True, None)]
)
def test_apify_price(minor, price):
    assert grab.apify_price(minor) == price


@pytest.mark.parametrize(
    "line",
    [
        "5-C75BL6WCMANWLT",
        "https://food.grab.com/vn/en/restaurant/online-delivery/5-C75BL6WCMANWLT",
        "https://food.grab.com/vn/vi/restaurant/desi-saigon-delivery/5-C75BL6WCMANWLT?sourceID=x",
        "  5-C75BL6WCMANWLT/  ",
    ],
)
def test_restaurant_id_from_id_or_link(line):
    assert grab.restaurant_id(line) == "5-C75BL6WCMANWLT"


@pytest.mark.parametrize("line", ["../../x", "5-c75bl6wcmanwlt", "5-SHORT", "https://evil.example/a/b", "5-ABC/../x"])
def test_odd_ids_are_refused(line):
    with pytest.raises(ValueError):
        grab.restaurant_id(line)


def test_ids_file_deduplicated(tmp_path):
    file = tmp_path / "ids.txt"
    file.write_text("5-C75BL6WCMANWLT\n\nhttps://food.grab.com/vn/en/restaurant/online-delivery/5-C75BL6WCMANWLT\n")
    assert grab.read_ids(["5-CYVVG74JRBM1LN"], [file]) == ["5-CYVVG74JRBM1LN", "5-C75BL6WCMANWLT"]


@pytest.mark.parametrize("argv", [["grab", "menus"], ["grab", "menus", "--id", "../../x"]])
def test_bad_input_is_refused_before_any_browser(argv):
    assert main(argv) == 2


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.mark.parametrize("body", [ValueError("empty body"), None, [], {"merchant": None}, {"errorDialog": {}}])
def test_an_answer_without_a_restaurant_is_skipped(body):
    # the page's first merchant answer was empty in the DC-09 live run; the next one carried the restaurant
    assert grab.merchant_of(FakeResponse(body)) is None


def test_an_answer_with_a_restaurant_is_taken():
    assert grab.merchant_of(FakeResponse(FIXTURE["full"])) is FIXTURE["full"]


def test_fixture_holds_no_session_tokens():
    assert "token" not in FIXTURE_PATH.read_text(encoding="utf-8").lower()
