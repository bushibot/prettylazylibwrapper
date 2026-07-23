# PrettyLazyLibWrapper

A friendlier frontend for [LazyLibrarian](https://gitlab.com/LazyLibrarian/LazyLibrarian):
a clean, family-friendly book/audiobook request page, plus a built-in
AudioBookBay Torznab indexer bridge, in one container.

> **⚠️ LAN / Tailscale use only.** There is no login on this app — anyone who
> can reach it on your network can submit requests, change settings, or read
> the AudioBookBay bridge's API key. It's built for a trusted home network or
> a Tailscale tailnet, not for exposing directly to the public internet. If
> you want that, you'd need to add your own auth (e.g. a reverse proxy with
> basic auth) in front of it — this project doesn't include any account
> management.

## What it does

- **Request page**: search Audible (audiobooks) or GoodReads (ebooks)
  directly for clean metadata and cover art, request with one click, track
  status (Wanted → Snatched → Downloaded) per household member, with a live
  download-progress bar sourced from qBittorrent/SABnzbd.
- **AudioBookBay bridge** (`/abb/api`): AudioBookBay has no real API, so this
  scrapes it and exposes a Torznab-compatible endpoint that Prowlarr (or
  LazyLibrarian directly) can query like any other indexer.
- **Settings screen** (`/config`): everything is configurable from the web UI
  after first boot, no need to recreate the container to change credentials.

## Requirements

- **LazyLibrarian**, already running and reachable, with an API key generated
  (Settings → Web Interface).
- **Recommended**: [Audiobookshelf](https://www.audiobookshelf.org/) or
  similar, to actually read/listen to what gets downloaded. This app only
  handles requesting and tracking, not playback.
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

Then install "PrettyLazyLibWrapper" from the Apps tab. Fill in the LazyLibrarian
fields at minimum; everything else can be configured later from `/config`.

### Docker Compose

```yaml
services:
  prettylazylibwrapper:
    image: ghcr.io/bushibot/prettylazylibwrapper:latest
    ports:
      - "80:80"
    volumes:
      - ./data:/data
      - /path/to/lazylibrarian/lazylibrarian.db:/ll-db/lazylibrarian.db
    environment:
      LL_URL: "http://lazylibrarian:5299"
      LL_API_KEY: "your-ll-api-key"
```

Everything else (qBittorrent, SABnzbd, users, the AudioBookBay bridge) can be
set from `/config` after first boot instead of environment variables, if you'd
rather not bake secrets into your compose file.

## Configuration reference

| Setting | Required | Notes |
|---|---|---|
| `LL_URL` | yes | LazyLibrarian's base URL |
| `LL_API_KEY` | yes | From LazyLibrarian's Settings → Web Interface |
| `USERS` | no | Comma-separated household names, e.g. `Alex,Sam,Jordan` |
| `QBIT_URL` / `QBIT_USER` / `QBIT_PASS` | no | For live download progress |
| `SAB_URL` / `SAB_API_KEY` | no | Alternative/additional to qBittorrent |
| `ABB_BASE` | no | Defaults to `https://audiobookbay.lu` |
| `BRIDGE_API_KEY` | no | Set this and use the same value in Prowlarr's indexer config |

All of the above can also be set later from `/config` — env vars only seed
the initial value on first boot.

## License

MIT
