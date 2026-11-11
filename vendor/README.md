# Vendored Code

Open-source code copied into this repo and owned by us (BR-DEP-01): pinned
here, modified freely, every change listed. Re-review per
`.claude/security.md` on every re-sync.

## tiktok-scraper
- Origin: https://github.com/omkarcloud/tiktok-scraper
- Commit: `683959302488fd773b56e2cf737e591bf12aa4be` (2026-10-23), copied
  2026-10-31
- Licence: MIT (`tiktok-scraper/LICENSE`, © 2026 Chetan Jain). Its signer is
  ported from Evil0ctal/Douyin_TikTok_Download_API (Apache-2.0).
- Used by: `crawler/sources/tiktok` (from DC-03); `spikes/` until then.
- Needs `curl_cffi`. `bottle`, `cheroot` and `marshmallow` only serve its own
  HTTP API, which we don't use.

### Security review (2026-10-31, before the first run)
- Network: only `www.tiktok.com`, `webcast.tiktok.com`, `library.tiktok.com`
  and `ads.tiktok.com` (constants in `tiktok/fetch.py`), optionally through
  `TIKTOK_PROXY`. No telemetry; nothing goes to omkarcloud or anyone else.
- Files: writes only debug dumps when `TIKTOK_DEBUG_DIR` is set
  (`fetch.dump_debug`) and `main.py`'s `output/`.
- No `subprocess`, `exec`, `eval`, `os.system` or pickle anywhere.
- Read line by line: `config.py`, `tiktok/search.py`, `tiktok/fetch.py`
  (sessions, `api()`, `_api_once()`). `tiktok/signer.py`: stdlib-only
  imports (`base64`, `hashlib`, `random`, `time`), pure computation — not
  read line by line.

### Our changes
None yet.

## google-maps-scraper (ported, not vendored)
- Origin: https://github.com/gosom/google-maps-scraper, tag `v1.18.1`
  (2026-09-20). Licence: MIT.
- Taken: the index map of a Google Maps place array (`gmaps/entry.go`
  `EntryFromJSON`, `getHours`; `gmaps/multiple.go`), ported by hand into
  `crawler/sources/gmaps.py` (`at`, `to_item`, `opening_hours`). No file is
  copied, and none of its code runs here.
- Not taken: the release binary (61 MB). It also ships PostHog telemetry
  (`tlmt/goposthog`), a web UI, a Postgres queue and cloud provisioning;
  see `.claude/architecture.md`, DC-05 amendment.
- Changed against gosom: the closed state is read from `[23]` (permanently)
  and `[160][0]` (temporarily); gosom's `[34][4][4]` and `[88][0]` don't
  carry it in Vietnamese results. The address comes from `[39]`, the same
  string Apify gives. Re-check the index map when Google changes its
  payload: the contract test in `tests/test_gmaps.py` uses real payloads.
