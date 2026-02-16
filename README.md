# lunaris

A complete offline mirror of [lunaris.moe](https://lunaris.moe), a Genshin Impact character database and build planner.

This repository contains a full local copy of the lunaris.moe website (frontend + API data) that can be browsed entirely offline. Just start the local server and open your browser.

[Chinese / 中文文档](README.zh-CN.md)

## Quick Start

Requirements: Python 3.9+, [httpx](https://www.python-httpx.org/)

```bash
pip install httpx starlette hypercorn
python lunaris.py serve
```

Open `http://localhost:9000/` in your browser. Done.

## Local Server

```bash
python lunaris.py serve
```

Starts a local HTTP server serving the mirrored site. The server automatically rewrites hardcoded `https://lunaris.moe` and `https://api.lunaris.moe` URLs to point to localhost, so the site works seamlessly offline.

The server supports two engines, selected automatically at startup:

- **ASGI mode** (recommended): Uses [Starlette](https://www.starlette.io/) + [Hypercorn](https://hypercorn.readthedocs.io/) for async I/O and high concurrency. Install with `pip install starlette hypercorn`.
- **stdlib mode** (fallback): Uses Python's built-in `http.server` with no extra dependencies. Sufficient for single-user browsing.

Both modes include ETag/304 conditional requests and tiered Cache-Control headers. For gzip compression, run `python lunaris.py precompress` first to generate `.gz` sidecar files -- the server will serve these directly when the client supports gzip, avoiding runtime compression overhead.

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--port N` | 9000 | Listen port (or `LUNARIS_SERVE_PORT` env var) |
| `--bind ADDR` | 0.0.0.0 | Bind address |
| `--no-rewrite` | rewrite on | Disable URL rewriting |

Endpoints:

- `http://localhost:9000/` -- Browse the site
- `http://localhost:9000/__api__/...` -- API data
- `http://localhost:9000/__verify__/stats` -- Request statistics
- `http://localhost:9000/__verify__/unexpected-missing` -- Missing resource report

## Mirrored Data

- Frontend: ~9,800 files (HTML, JS, CSS, fonts, images)
- API: ~5,600 files (JSON data, character icons, weapon/artifact assets)
- Game data versions: 6.3.52, 6.3.53, 6.3.54
- Languages: English, Chinese (Simplified), Russian, Portuguese

## Generate Checksums

```bash
python lunaris.py sha256
```

Generates `SHA256SUMS.tsv` with hash, size, and path for every file under `site/` and `api/`.

## Project Structure

```
lunaris.py              CLI entry point
lunaris/
  common.py             Shared constants, utilities, Downloader
  backup.py             Phase 1: HTTP crawl
  replay.py             Phase 2: Playwright dynamic replay
  closure.py            Phase 3: Closure verification
  scan.py               Phase 4: Full-site Playwright scan
  audit.py              Phase 5: UI interaction audit
  server.py             Local server
site/                   Mirrored frontend (lunaris.moe)
api/                    Mirrored API data (api.lunaris.moe)
manifest.jsonl          Download log (URL, status, hash)
SHA256SUMS.tsv          File integrity checksums
```

## Re-crawling from Scratch

The mirror data in this repository is already complete. You should not need to re-crawl unless the upstream site has been updated and you want a fresh snapshot.

Additional requirement: [Playwright](https://playwright.dev/python/)

```bash
pip install playwright
playwright install chromium
```

```bash
python lunaris.py backup
```

This runs a 5-phase pipeline:

1. **HTTP Crawl** -- Downloads seed URLs, frontend assets, API version lists, character data, and semantically derived asset URLs.
2. **Dynamic Replay** -- Visits sampled pages with Playwright, capturing runtime network requests to discover additional resources.
3. **Closure Verification** -- Scans all downloaded files for embedded URLs and fills in any gaps.
4. **Full-Site Scan** -- Visits every route with Playwright, logging failures and downloading missing resources.
5. **UI Interaction Audit** -- Clicks tabs, expands collapsibles, and scrolls pages to trigger lazy-loaded assets.

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--concurrency N` | 12 | Download concurrency |
| `--port N` | 9001 | Local server port for UI audit phase |
| `--skip-browser` | off | Skip Playwright phases (HTTP crawl only) |

## Pre-compression

```bash
python lunaris.py precompress
```

Scans `site/` and `api/` for compressible text files (`.js .css .html .json .svg .txt .xml`) and generates `.gz` sidecar files at maximum compression level. Files ≤256 bytes are skipped. Re-running only compresses files that are newer than their existing `.gz` counterpart.

## Server Tuning

When using ASGI mode, additional flags are available:

| Flag | Default | Description |
|------|---------|-------------|
| `-w, --workers N` | min(4, CPU cores) | Hypercorn worker count |
| `--backlog N` | 512 | Connection backlog size |
| `--access-log PATH` | off | Access log path (`-` for stdout) |

## Credits

[lunaris.moe](https://lunaris.moe) is created by Kuroo. This repository contains only the offline mirroring toolkit and the mirrored data; all original content belongs to its respective authors.
