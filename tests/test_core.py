import json
import re
from datetime import UTC, datetime

import pytest

from crawler import core
from crawler.__main__ import main


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_id_starts_with_its_utc_start_time():
    assert re.fullmatch(r"20261031T150405Z-[0-9a-f]{4}", core.new_run_id(datetime(2026, 10, 31, 15, 4, 5, tzinfo=UTC)))


def test_write_json_is_whole_and_never_overwrites_by_default(tmp_path):
    path = tmp_path / "raw" / "run.json"
    core.write_json(path, {"text": "ăn gì đà lạt"})
    assert read(path) == {"text": "ăn gì đà lạt"}
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(FileExistsError):
        core.write_json(path, {"text": "other"})
    assert read(path) == {"text": "ăn gì đà lạt"}
    core.write_json(path, {"text": "other"}, overwrite=True)
    assert read(path) == {"text": "other"}


@pytest.mark.parametrize("bad", ["../../x", "12/34", "12\\34", "..", "", " 1", "1" * 26, "7627708655311408391.vtt"])
def test_id_path_rejects_anything_but_a_plain_id(tmp_path, bad):
    with pytest.raises(ValueError):
        core.id_path(tmp_path, bad, ".vtt")


def test_id_path_accepts_tiktok_ids(tmp_path):
    assert core.id_path(tmp_path, "7627708655311408391", ".vtt") == tmp_path / "7627708655311408391.vtt"


def test_successful_run_writes_the_envelope_and_native_pages(tmp_path):
    with core.Run("tiktok", {"keywords": ["ăn gì đà lạt"]}, root=tmp_path) as run:
        run.items += [
            {"id": "1", "searchQuery": "a", "text": "first"},
            {"id": "2", "searchQuery": "a"},
            {"id": "1", "searchQuery": "a", "text": "again"},  # same video, same keyword: dropped
            {"id": "1", "searchQuery": "b"},  # same video, another keyword: kept
            {"no": "id"},
            {"no": "id"},
        ]
        run.native.append({"label": {"page": 0}, "payload": {"item_list": []}})
    saved = read(run.path)
    assert run.path == tmp_path / "tiktok" / f"{run.id}.json"
    assert saved["run"]["id"] == run.id and saved["run"]["status"] == "SUCCEEDED"
    assert saved["run"]["input"] == {"keywords": ["ăn gì đà lạt"]}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", saved["run"]["at"])
    assert saved["items"] == [
        {"id": "1", "searchQuery": "a", "text": "first"},
        {"id": "2", "searchQuery": "a"},
        {"id": "1", "searchQuery": "b"},
        {"no": "id"},
        {"no": "id"},
    ]
    assert read(tmp_path / "tiktok" / "native" / f"{run.id}.json")["pages"] == run.native


def test_failed_run_keeps_what_it_collected_and_still_raises(tmp_path):
    with pytest.raises(RuntimeError, match="blocked"), core.Run("tiktok", {}, root=tmp_path) as run:
        run.items.append({"id": "1"})
        raise RuntimeError("blocked")
    saved = read(run.path)
    assert saved["run"]["status"] == "FAILED" and saved["items"] == [{"id": "1"}]


def test_interrupted_run_is_saved_as_aborted(tmp_path):
    with pytest.raises(KeyboardInterrupt), core.Run("tiktok", {}, root=tmp_path) as run:
        raise KeyboardInterrupt
    assert read(run.path)["run"]["status"] == "ABORTED"


def test_run_without_native_pages_writes_no_native_file(tmp_path):
    with core.Run("tiktok", {}, root=tmp_path):
        pass
    assert not (tmp_path / "tiktok" / "native").exists()


def test_cli_without_a_source_is_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_due_keys_for_a_weekly_refresh():
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    done = {
        "a": {"at": "2026-11-01T12:00:00Z"},
        "b": {"at": "2026-10-20T12:00:00Z"},
        "c": {"at": "2026-10-26T12:00:00Z"},
    }
    assert core.due(["a", "b", "c", "d"], done) == ["d"]  # never done only
    assert core.due(["a", "b", "c", "d"], done, 7, now) == ["b", "d"]  # c is exactly 7 days old: not yet
    assert core.due(["a", "b", "c", "d"], done, 0.5, now) == ["a", "b", "c", "d"]
