"""Site-agnostic plumbing every source uses: run ids, raw files written whole, id-safe file names."""

import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

DATA_RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
DIGITS_ID = re.compile(r"\d{1,25}")  # TikTok video ids (security.md)

log = logging.getLogger(__name__)


def new_run_id(now: datetime | None = None) -> str:
    """UTC start time plus a short random suffix — sortable and unique (naming.md): 20261031T150405Z-3f9a."""
    return f"{now or datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def write_text(path: Path, text: str, *, overwrite: bool = False) -> None:
    """Write through a temp file and os.replace, so a crash never leaves half a file. Raw files are never replaced
    unless the caller asks (BR-RAW-01)."""
    # ponytail: exists-then-replace isn't atomic against a second writer; one run id has one writer
    if not overwrite and path.exists():
        raise FileExistsError(f"{path} exists — raw files are never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, data: object, *, overwrite: bool = False) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False), overwrite=overwrite)


def read_done(index: Path) -> dict:
    """A source's resume index (data/state/<source>-*.json): what earlier runs finished, by keyword or query."""
    return json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}


def mark_done(index: Path, key: str, results: int, run_id: str) -> None:
    done = read_done(index)
    done[key] = {"at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}", "results": results, "runId": run_id}
    write_json(index, done, overwrite=True)


def due(keys: list[str], done: dict, older_than_days: float | None = None, now: datetime | None = None) -> list[str]:
    """The keys never done, plus — with `older_than_days` — those last done longer ago than that (the weekly refresh,
    owner 2026-11-02). Unlike --refresh it survives the per-run cap: what a run already redid isn't due again."""
    if older_than_days is None:
        return [key for key in keys if key not in done]
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    return [key for key in keys if key not in done or datetime.fromisoformat(done[key]["at"]) < cutoff]


def id_path(directory: Path, item_id: str, suffix: str) -> Path:
    """directory/<id><suffix>, once the id is exactly what the site's ids look like — a scraped `../../x` must
    never become a path (security.md)."""
    if not DIGITS_ID.fullmatch(item_id):
        raise ValueError(f"unsafe id for a file name: {item_id!r}")
    return directory / f"{item_id}{suffix}"


class Run:
    """One crawl run. However the `with` block ends, what was collected is written on the way out:
    SUCCEEDED, ABORTED (Ctrl+C) or FAILED (any other error, which still propagates) — data-guideline.md.

    items  — output items in the Apify actor's shape (docs/data/output-contract.md), one per id and keyword
    native — the site's own payloads, kept unfiltered next to them (BR-RAW-01)
    """

    def __init__(self, source: str, run_input: dict, root: Path = DATA_RAW) -> None:
        self.source, self.input, self.root = source, run_input, root
        self.started = datetime.now(UTC)
        self.id = new_run_id(self.started)
        self.path = root / source / f"{self.id}.json"
        self.items: list[dict] = []
        self.native: list[object] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind, error, traceback) -> bool:
        status = "SUCCEEDED" if kind is None else "ABORTED" if issubclass(kind, KeyboardInterrupt) else "FAILED"
        run = {"id": self.id, "status": status, "at": f"{self.started:%Y-%m-%dT%H:%M:%SZ}", "input": self.input}
        items, seen = [], set()
        # First copy per id and keyword: the same video found by two keywords stays twice, as Apify returns it and as
        # FoodReview needs it (each keyword ties the clip to a district). Items without an id all stay.
        for item in self.items:
            key = (item.get("id"), item.get("searchQuery"))
            if key[0] is None or key not in seen:
                seen.add(key)
                items.append(item)
        write_json(self.path, {"run": run, "items": items})
        if self.native:
            write_json(self.root / self.source / "native" / f"{self.id}.json", {"run": run, "pages": self.native})
        log.info("run %s %s: %d items → %s", self.id, status, len(items), self.path)
        return False  # never swallow the error
