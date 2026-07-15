# AudioBookRequest revamp — implementation plan

Fork: https://github.com/bk-bf/AudioBookRequest (upstream: markbeep/AudioBookRequest)

## Goal
Turn ABR from a "search + hand off to Prowlarr" tool into a Radarr-style manager that
owns the download client, tracks real progress, imports finished files into the
Audiobookshelf library, and shows a proper detail page per book.

## The core problem
ABR never talks to a download client. `start_download()` POSTs a release `guid` to
Prowlarr's `/api/v1/search`, Prowlarr hands it to *its* client, and ABR immediately sets
`downloaded=True` on the 200 response. There is no queue, no progress, no importer.
`downloaded` means "Prowlarr accepted the grab," not "the file is in the library."

Everything below is about closing that gap the way Radarr does: ABR's own download-client
integration + queue monitor + importer.

---

## Phase 0 — Ranking: toggleable seeder priority (quick win)
**Ask:** keep `min_seeders` floor as-is; add a toggle that makes auto-download prefer the
most-seeded variant (seeders + leechers), so 3–5-seed audiobooks still win when they're
the best available.

Changes:
- `app/internal/ranking/quality.py`
  - New config keys: `quality_seeder_priority` (bool), `quality_seeder_use_leechers` (bool).
  - Add `get/set_seeder_priority`, `get/set_seeder_use_leechers`; register in `reset_all`.
  - **Do NOT touch** `quality_min_seeders` — floor stays.
- `app/internal/ranking/download_ranking.py`
  - `_compare_seeders`: when `seeder_use_leechers`, compare `seeders+leechers` instead of
    `seeders`.
  - `CompareSource.__init__`: when `seeder_priority` is on, move `_compare_seeders` to the
    front of `compare_order` (right after `_compare_valid`), so among valid matches the
    most-seeded wins before format/indexer tiebreakers. Off = current behaviour (seeders
    last). Torrent-vs-usenet guard stays.
- `app/routers/api/settings/download.py` + `app/routers/pages/settings/download.py` +
  `templates/pages/Settings/*` (download settings): two checkboxes. Extend
  `DownloadSettings` / `UpdateDownloadSettings` models.

No migration (config is key/value rows in the `Config` table). ~half a day.

---

## Phase 1 — Download-client integration (the foundation)
Client: **qBittorrent** first (matches the mediaserver stack; torrent-only to start).
Design the interface so SABnzbd/others can be added later.

### 1a. Config
New `app/internal/download_client/config.py` (mirror `audiobookshelf/config.py`,
`StringConfigCache`):
- `dc_type` (`qbittorrent`), `dc_base_url`, `dc_username`, `dc_password`,
  `dc_category` (default `audiobookrequest`), `dc_download_dir` (client-side completed
  path as ABR sees it), `dc_enabled`.
- `is_valid()` / `raise_if_invalid()`.
- Secrets: password stored via existing `Config` table + `censor` util for display.

### 1b. Client abstraction
`app/internal/download_client/abstract.py` — `DownloadClient` ABC:
- `add(source, save_dir, category) -> download_id` (info-hash for torrents)
- `list_items(category) -> list[DownloadStatus]`
- `get_item(download_id) -> DownloadStatus | None`
- `remove(download_id, delete_files)`

`DownloadStatus` model: `download_id`, `name`, `progress` (0–1), `state`
(`queued|downloading|completed|stalled|error|seeding`), `eta`, `dlspeed`, `size`,
`content_path` (where finished files live).

`app/internal/download_client/qbittorrent.py`:
- Auth via `/api/v2/auth/login` (cookie), `torrents/add` (magnet or .torrent upload with
  `category`, `savepath`), `torrents/info?category=`, map qBit states → our enum.
- Match by info-hash — **already computed** in `prowlarr.py:123-131`; stop throwing it
  away and persist it (see 1c).

### 1c. Data model — track real downloads
New Alembic migration + `DownloadQueueItem` (SQLModel table):
- `id`, `asin_or_uuid` (FK-ish link to Audiobook/ManualBookRequest), `download_id`
  (info-hash), `source_title`, `indexer`, `protocol`, `size`, `state`, `progress`,
  `save_path`, `error`, `created_at`, `updated_at`, `imported` (bool).
- Add `downloaded_path: str | None` to `Audiobook` / `ManualBookRequest` (where the
  imported files landed — surfaced on the detail page).

### 1d. Rewire the grab path
`start_download()` / `query.py`:
- When a download client is configured & enabled → ABR adds the release **directly to
  qBittorrent** (using `download_url`/`magnet_url` from the ranked `ProwlarrSource`) with
  ABR's category + save dir, instead of POSTing to Prowlarr. Fall back to the current
  Prowlarr hand-off when no client is configured (keeps upstream behaviour working).
- **Stop** setting `downloaded=True` here. Create a `DownloadQueueItem(state=queued)`
  instead. `downloaded` now flips only after successful import (Phase 2).

### 1e. Queue monitor (background poller)
`app/internal/download_client/monitor.py` + register a startup task in `app/main.py`
(asyncio loop; no scheduler dep needed, or add APScheduler if preferred):
- Every N seconds: for each non-imported `DownloadQueueItem`, pull status from the client,
  update `progress`/`state`, and on completion trigger the importer (Phase 2).
- One asyncio task, guarded so only one runs; configurable interval.

~3–5 days.

---

## Phase 2 — Importer (files into the library, Radarr-style)
`app/internal/download_client/importer.py`:
- On completion: read `content_path` from the client, walk it for audio files
  (`.m4b/.mp3/.flac/.m4a/.ogg`), ignore samples/junk.
- **Hardlink** into the ABS library, falling back to copy if cross-filesystem
  (`os.link` → `shutil.copy2`). Hardlink so the torrent keeps seeding — exactly Radarr's
  behaviour.
- Organize with a naming scheme: `{Author}/{Title}/…` (configurable string later; start
  with a sensible default from the linked book's metadata).
- Set `Audiobook.downloaded_path`, flip `downloaded=True`, mark queue item `imported`,
  then trigger the existing `abs_trigger_scan()`.
- Fire the existing `on_successful_download` notification here (real completion, not
  optimistic).

**Deployment note (mediaserver / Docker):** hardlinks require the qBit completed-downloads
dir and the ABS library dir to be the **same filesystem/volume**, mounted into the ABR
container with identical paths (TRaSH single-`/data`-mount layout). `dc_download_dir` must
be the path *as ABR sees it*. Document this; without it, imports silently fall back to slow
copies or fail. Auth is OFF on this stack — no extra creds needed inside the tailnet.

~2–4 days (overlaps Phase 1).

---

## Phase 3 — Auto-download that actually works
**Ask:** stop needing the manual "auto download" button in the wishlist.

- `app/internal/download_client/monitor.py` (or a sibling task): periodic sweep over
  wishlist items where `downloaded=False` and no active `DownloadQueueItem` → re-query
  Prowlarr, rank, and if a valid match exists, grab the top source automatically.
- Gate on the existing `quality_auto_download` toggle; respect min_seeders floor + the new
  seeder-priority toggle from Phase 0.
- Retry with backoff so a book with no source today gets picked up when one appears.
- Keep the manual button as a "grab now" override.

~2 days (reuses Phase 1 plumbing).

---

## Phase 4 — Wishlist progress UI
- `templates/pages/Wishlist/*`: per-row status pill — `Queued / 42% ↓ / Importing /
  Downloaded / Failed` — driven by `DownloadQueueItem`.
- HTMX poll (the app already uses HTMX) on active rows to refresh progress; stop polling
  once imported.
- Extend the wishlist API (`requests.py list_requests` / a new
  `/requests/{id}/status`) to join queue state into results.

~2 days.

---

## Phase 5 — Proper detail page (replaces the Audible redirect)
**Ask:** clicking a book opens an internal Sonarr/Radarr-style page, not audible.com.

- `BookCard.jinja:53`: change the outbound `href="https://audible…"` to an internal
  `{base_url}/book/{asin}` (keep an "View on Audible" secondary link).
- New route `app/routers/pages/book.py` + `templates/pages/Book/Index.jinja`:
  cover, full metadata, runtime, requesters, current download/queue state + progress,
  imported **file path**, ranked source list with a manual grab, history
  (requested → grabbed → imported), refresh-sources button.
- Reuses `get_wishlist_results`, `query_sources`, and the new `DownloadQueueItem`.

~2–3 days.

---

## Suggested order
0 (ranking toggle) → 1 (qBit client + queue model) → 4 (progress UI, immediately
gratifying) → 2 (importer) → 3 (auto-download poller) → 5 (detail page).

## Cross-cutting
- Keep upstream behaviour intact when no download client is configured (fall back to the
  Prowlarr hand-off) so the fork stays mergeable/contributable upstream.
- Tests: `app/**/test_*.py` pattern exists — add unit tests for ranking toggle, qBit state
  mapping, importer path logic (hardlink vs copy).
- Migrations via Alembic (`alembic/versions/`), matching existing style.
