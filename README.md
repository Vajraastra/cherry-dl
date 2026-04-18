# cherry-dl

Mass downloader for content platforms — Kemono, Patreon, and Pixiv. Manages profiles per artist, tracks what you already have, avoids duplicates, and syncs incrementally so repeated runs only fetch what's new.

---

## Interfaces

**TUI** (default) — terminal UI built with Textual. Full profile management, per-artist download control, batch mode, and duplicate detection — all from the terminal.

**CLI** — scriptable commands for automation and headless use.

**GUI** (legacy) — PySide6 desktop interface, still functional but superseded by the TUI.

---

## Features

- **Profile-based** — each artist is a profile. Add multiple URLs per profile (Kemono + Patreon + Pixiv for the same artist) and they all sync to one folder.
- **Incremental sync** — tracks `last_synced` per URL. Re-running only fetches posts newer than the last successful sync.
- **Deduplication** — SHA-256 hash registry per catalog. Files already downloaded are never re-downloaded, even across sites.
- **Duplicate detection** — cross-profile hash comparison via SQL ATTACH join. Detects and merges duplicate profiles, migrates unique files, and optionally compacts numbering.
- **File type filtering** — include or exclude by extension group (images, video, audio, archives, etc.), configurable per profile or globally in batch mode.
- **Batch mode** — queues all profiles and downloads sequentially with progress tracking, consecutive-error detection, and graceful stop.
- **Compaction** — renumbers files to close gaps left after purging unwanted downloads, with dry-run preview and SHA-256 verification.
- **Atomic writes** — downloads to `.tmp`, renames on success. Orphan `.tmp` files are cleaned on next run.
- **SQLite WAL** — concurrent workers share one catalog without locking conflicts.

---

## Supported sites

| Site | Auth method |
|---|---|
| Kemono | No auth required |
| Patreon | Browser cookies (auto-detected) |
| Pixiv | Browser cookies (auto-detected) |

---

## Installation

```bash
git clone https://github.com/Vajraastra/cherry-dl.git
cd cherry-dl
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Or use the included `run.sh` (creates venv, installs deps, launches TUI):

```bash
chmod +x run.sh && ./run.sh
```

---

## CLI reference

```bash
cherry-dl tui                            # Launch TUI (default)
cherry-dl download <url>                 # One-off download
cherry-dl download <url> --workers 5
cherry-dl status                         # Show all profiles
cherry-dl config set download_dir /path
cherry-dl config show
cherry-dl compact <profile>              # Renumber files, close gaps
cherry-dl compact <profile> --dry-run
cherry-dl migrate-structure              # Migrate to artist-first folder layout
cherry-dl migrate-pending                # Initialize pending queues on existing catalogs
```

---

## License

Business Source License 1.1 — free for non-commercial use.  
Commercial use requires a separate license. See [LICENSE](LICENSE) for details.  
Converts to MIT on 2030-04-18.
