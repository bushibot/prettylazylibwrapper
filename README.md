# PrettyLazyLibWrapper

A friendlier frontend for [LazyLibrarian](https://gitlab.com/LazyLibrarian/LazyLibrarian)
or [Shelfarr](https://shelfarr.org): a clean, family-friendly book/audiobook
request page, plus a built-in AudioBookBay Torznab indexer bridge, in one
container. The acquisition backend is pluggable and switchable live from
`/config` (no redeploy) — see [Backends](#backends-lazylibrarian-vs-shelfarr)
below for what actually differs between them.

> **⚠️ LAN / Tailscale use only.** There is no login on this app — anyone who
> can reach it on your network can submit requests, change settings, or read
> the AudioBookBay bridge's API key. It's built for a trusted home network or
> a Tailscale tailnet, not for exposing directly to the public internet. If
> you want that, you'd need to add your own auth (e.g. a reverse proxy with
> basic auth) in front of it — this project doesn't include any account
> management.

## What it does

- **Request page**: search Audible (audiobooks) — always, regardless of
  backend — or your configured ebook source (GoodReads via LazyLibrarian, or
  OpenLibrary/Google Books/Hardcover via Shelfarr) for clean metadata and
  cover art, request with one click, track status (Wanted → Snatched →
  Downloaded) per household member, with a live download-progress bar
  sourced from qBittorrent/SABnzbd.
- **Author/series watch-list**: follow an author or series (a "Follows" tab,
  plus a quick "+ Follow" button right on audiobook search results) and a
  background job checks for new releases automatically, auto-requesting
  anything genuinely new — capped per check pass so a prolific author or a
  big backlog can't flood your download queue in one go. Preorders are held
  until their actual release date instead of being requested (and retried
  against indexers) months early. This lives in the app itself, independent
  of which backend is active — see [Backends](#backends-lazylibrarian-vs-shelfarr).
- **AudioBookBay bridge** (`/abb/api`): AudioBookBay has no real API, so this
  scrapes it and exposes a Torznab-compatible endpoint that Prowlarr (or
  LazyLibrarian directly) can query like any other indexer. Backend-agnostic:
  Shelfarr picks it up automatically if it's already in Prowlarr's indexer
  list.
- **Discovery browsing**: a genre-filterable New Releases / Pre-orders wall for
  audiobooks (Audible), plus a NYT bestsellers row for both audiobooks and
  ebooks (optional - needs a free NYT API key). Click straight through to
  request anything you see.
- **Settings screen** (`/config`): everything is configurable from the web UI
  after first boot, no need to recreate the container to change credentials
  — including which backend is active.

## Backends: LazyLibrarian vs Shelfarr

`BACKEND` (`lazylibrarian` or `shelfarr`) picks which system actually
resolves catalog IDs, submits searches, and tracks download status. Switch
it any time from `/config` — takes effect on the very next request, no
restart needed. Already-in-flight requests keep tracking against whichever
backend actually created them, so switching mid-flight is safe.

| | LazyLibrarian | Shelfarr |
|---|---|---|
| Ebook search source | GoodReads | OpenLibrary / Google Books / Hardcover (per what's enabled) |
| Audiobook search source | Audible (same either way) | Audible (same either way) |
| Author/series monitoring | None built in — use this app's watch-list | None built in — use this app's watch-list |
| Already-owned check | Audiobookshelf, direct API (`ABS_URL`/`ABS_API_KEY`) | Same — independent of backend |
| Preorder handling | Held server-side by LL until release date | Held by this app's watch-list until release date (manual requests for a preorder still submit immediately) |
| Failed-search retry | LL re-searches every `Wanted` book on its own cron | This app retries anything stuck `not_found`/`failed` for 6h+ |
| Connection needed | `LL_URL` + `LL_API_KEY`, plus a **read-only mount** of LL's own SQLite DB | `SHELFARR_URL` + `SHELFARR_API_KEY` only — no DB mount |
| Write discipline | Only ever through LL's own API commands, never direct DB writes (see the DB-corruption note in [LAZYLIBRARIAN-SETUP.md](LAZYLIBRARIAN-SETUP.md)) | REST API only — no DB access at all |

Both backends actually deliver files through the same Torznab indexer stack
(Prowlarr, including the AudioBookBay bridge above) — the difference is
purely in how each *manages* requests and metadata, not where content comes
from.

## Requirements

- **Either LazyLibrarian or Shelfarr**, already running and reachable, with
  an API key generated for each (LL: Settings → Web Interface. Shelfarr:
  Admin → Settings → API Tokens).
- **Audiobookshelf** (`ABS_URL`/`ABS_API_KEY`/library IDs) — used for the
  already-owned check regardless of backend, and recommended anyway as the
  actual place to read/listen to what gets downloaded. This app only handles
  requesting and tracking, not playback.
- qBittorrent and/or SABnzbd, for live download-progress tracking (optional —
  the app works without it, you just won't get a progress bar).
- If you want the AudioBookBay bridge to actually be used, add it as a custom
  Torznab indexer in Prowlarr (or directly in LazyLibrarian) pointing at
  `http://<this container's IP>/abb/api`, using the `BRIDGE_API_KEY` you set
  below.

## Running it

### Unraid (Community Applications template)

Add this repo as a template source in Unraid: **Docker → Add Container →
Template repositories**, add:

```
https://github.com/bushibot/prettylazylibwrapper
```

Then install "PrettyLazyLibWrapper" from the Apps tab. Fill in your chosen
backend's fields at minimum; everything else can be configured later from
`/config`, including switching `BACKEND` itself.

### Docker Compose

```yaml
services:
  prettylazylibwrapper:
    image: ghcr.io/bushibot/prettylazylibwrapper:latest
    ports:
      - "80:80"
    volumes:
      - ./data:/data
      # Only needed if BACKEND=lazylibrarian:
      - /path/to/lazylibrarian/lazylibrarian.db:/ll-db/lazylibrarian.db
    environment:
      BACKEND: "lazylibrarian"   # or "shelfarr"
      # LazyLibrarian backend:
      LL_URL: "http://lazylibrarian:5299"
      LL_API_KEY: "your-ll-api-key"
      # Shelfarr backend (use instead of the two above if BACKEND=shelfarr):
      # SHELFARR_URL: "http://shelfarr:80"
      # SHELFARR_API_KEY: "your-shelfarr-api-token"
      # Already-owned check, independent of backend:
      ABS_URL: "http://audiobookshelf:80"
      ABS_API_KEY: "your-abs-api-token"
      ABS_AUDIOBOOK_LIBRARY_ID: "your-audiobook-library-id"
      ABS_EBOOK_LIBRARY_ID: "your-ebook-library-id"
```

Everything else (qBittorrent, SABnzbd, users, the AudioBookBay bridge) can be
set from `/config` after first boot instead of environment variables, if you'd
rather not bake secrets into your compose file.

## Configuration reference

| Setting | Required | Notes |
|---|---|---|
| `BACKEND` | no | `lazylibrarian` (default) or `shelfarr` — switchable live from `/config` |
| `LL_URL` | if `BACKEND=lazylibrarian` | LazyLibrarian's base URL |
| `LL_API_KEY` | if `BACKEND=lazylibrarian` | From LazyLibrarian's Settings → Web Interface |
| `SHELFARR_URL` | if `BACKEND=shelfarr` | Shelfarr's base URL |
| `SHELFARR_API_KEY` | if `BACKEND=shelfarr` | From Shelfarr's Admin → Settings → API Tokens |
| `ABS_URL` / `ABS_API_KEY` | no, but recommended | Audiobookshelf, used for the already-owned check regardless of backend |
| `ABS_AUDIOBOOK_LIBRARY_ID` / `ABS_EBOOK_LIBRARY_ID` | no | Which Audiobookshelf libraries to check against |
| `WATCHLIST_CHECK_INTERVAL_HOURS` | no | How often followed authors/series get re-checked (default `24`) |
| `WATCHLIST_MAX_NEW_PER_ITEM` | no | Safety cap on auto-requests per followed author/series per check (default `5`) — prevents a newly-followed prolific author from flooding your queue in one pass |
| `USERS` | no | Comma-separated household names, e.g. `Alex,Sam,Jordan` |
| `QBIT_URL` / `QBIT_USER` / `QBIT_PASS` | no | For live download progress |
| `SAB_URL` / `SAB_API_KEY` | no | Alternative/additional to qBittorrent |
| `ABB_BASE` | no | Defaults to `https://audiobookbay.lu` |
| `BRIDGE_API_KEY` | no | Set this and use the same value in Prowlarr's indexer config |
| `ABB_PROXY_URL` | no | HTTP proxy for reaching AudioBookBay, e.g. `http://host:port` - see below |
| `NYT_API_KEY` | no | Powers the ebook New Releases browse and the audiobook Bestsellers row - see below |

All of the above can also be set later from `/config` — env vars only seed
the initial value on first boot.

### If AudioBookBay searches are timing out

Some ISPs block `audiobookbay.lu` outright at the network level (confirmed:
Comcast in the US) - the connection just silently times out, no error page.
This looks to be ISP-specific, not a country-wide block: a US-based VPN exit
on a different network (a VPN provider's own datacenter, not a residential
ISP) reached the site fine, and with much better latency than routing
overseas - try that before assuming you need a different country. Point
`ABB_PROXY_URL` at an HTTP proxy on whatever exit actually works for you. A
small [gluetun](https://github.com/qdm12/gluetun)
sidecar with `HTTPPROXY=on` works well for this and doesn't need to touch any
other container's networking.

### Setting up the NYT API key

The ebook New Releases browse and the audiobook Bestsellers row both use the
NYT Books API. This was picked deliberately over the alternatives - GoodReads'
public API has been dead since 2020, and both Google Books and Open Library's
"sort by newest" turned out to be unreliable (mostly old reissues and, in
Open Library's case, outright bogus dates). A bestseller list can't contain
an unpublished book by definition, so NYT's data needed no extra filtering to
be genuinely current.

1. Register a free account at `developer.nytimes.com`
2. Go to **My Apps** → create a new app → enable **Books API** for it
3. Copy the generated key into `NYT_API_KEY` (env var or `/config`)

Free tier is 1,000 requests/day; this app caches responses for an hour
server-side, so normal use won't come close to that.

## LazyLibrarian configuration (if `BACKEND=lazylibrarian`)

When LL is the active backend, it does the actual searching, downloading and
filing — a correct LL configuration matters as much as the app itself.

See **[LAZYLIBRARIAN-SETUP.md](LAZYLIBRARIAN-SETUP.md)** for the full working
configuration: naming templates that match Audiobookshelf's parser, the
one-book-per-folder prerequisite (and the library corruption that follows without
it), metadata precedence, and known upstream bugs. (If you're on `BACKEND=shelfarr`
instead, folder/naming templates are configured in Shelfarr's own Admin →
Settings, not here.)

## License

MIT
