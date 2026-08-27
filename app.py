import hashlib
import ipaddress
import os
import re
import socket
import sqlite3
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel

from settings import (
    cfg, init_settings_db, get_users, set_users, set_setting,
    get_all_settings_masked, SETTING_FIELDS,
)
from abb_bridge import app as abb_app
from backends import get_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prettylazylibwrapper")

LL_DB_PATH = os.environ.get("LL_DB_PATH", "/ll-db/lazylibrarian.db")  # mounted read-write
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "/data/prettylazylibwrapper.db")
COVER_CACHE_DIR = os.environ.get("COVER_CACHE_DIR", "/data/cover_cache")

EVERYONE_TAG = "Everyone"

AUDIBLE_API = "https://api.audible.com/1.0/catalog/products"

app = FastAPI(title="PrettyLazyLibWrapper")
app.mount("/abb", abb_app)


# ---------- local (BookRequest's own) db ----------

def get_local_db():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_local_db():
    conn = get_local_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT NOT NULL,
            book_type TEXT NOT NULL,
            source_id TEXT NOT NULL,      -- audible ASIN (audiobook) or goodreads bookid (ebook)
            ll_bookid TEXT,               -- resolved LazyLibrarian/GoodReads bookid once known
            authorid TEXT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            cover_url TEXT,
            release_date TEXT,
            status TEXT DEFAULT 'submitted',   -- submitted, unresolved, wanted, snatched, downloaded
            created_at TEXT,
            updated_at TEXT,
            progress_pct REAL DEFAULT 0,
            download_health TEXT DEFAULT 'sending',  -- sending, downloading, slow, stalled, broken, done
            health_checked_at TEXT,
            UNIQUE(source_id, book_type)
        )
    """)
    # in case of upgrading an existing db that predates these columns
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    for col, default in [("progress_pct", "0"), ("download_health", "'sending'"), ("health_checked_at", "NULL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
    conn.commit()
    conn.close()


# ---------- acquisition backend (LazyLibrarian or Shelfarr) ----------
# Which one is active is a live setting (cfg.BACKEND), so switching is a
# /config change, not a redeploy. See backends.py for the actual LL/Shelfarr
# implementations - this module only calls the common Backend interface,
# with the one exception of LLBackend's "is the other format already
# actively requested" callback, which needs access to this app's own local
# db and so is wired up here rather than living in backends.py.

def normalize(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


init_settings_db()  # must exist before get_backend() below reads cfg.BACKEND


def _ll_other_format_active(ll_bookid, other_type):
    conn = get_local_db()
    try:
        return bool(conn.execute(
            "SELECT 1 FROM requests WHERE ll_bookid=? AND book_type=? AND status NOT IN ('unresolved')",
            (ll_bookid, other_type)
        ).fetchone())
    finally:
        conn.close()


def get_active_backend():
    """Resolved fresh on every call (both constructors are cheap - just
    attribute assignment, no I/O) so a BACKEND change made on /config takes
    effect on the very next request, no restart needed."""
    b = get_backend(cfg, LL_DB_PATH)
    if b.name == "lazylibrarian":
        b.attach_other_active_checker(_ll_other_format_active)
    return b


# ---------- already-owned check (Audiobookshelf, independent of backend) ----------
# Previously this was a fuzzy match against LazyLibrarian's own db - moved to
# ABS directly since that's the actual source of truth for what's owned
# regardless of which acquisition backend is active, and it closes a real
# gap Shelfarr's own duplicate-detection has (it only tracks books it
# acquired itself, not a household's pre-existing library - see
# scalable-greeting-pike.md).

def abs_already_have(title, author, book_type):
    lib_id = cfg.ABS_AUDIOBOOK_LIBRARY_ID if book_type == "audiobook" else cfg.ABS_EBOOK_LIBRARY_ID
    if not (cfg.ABS_URL and cfg.ABS_API_KEY and lib_id):
        return False
    try:
        r = requests.get(
            f"{cfg.ABS_URL.rstrip('/')}/api/libraries/{lib_id}/search",
            headers={"Authorization": f"Bearer {cfg.ABS_API_KEY}"},
            params={"q": title},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"ABS already-have check failed: {e}")
        return False

    target_title = normalize(title)
    target_author_last = normalize(author).split()[-1] if author else ""
    for entry in data.get("book", []):
        meta = (entry.get("libraryItem") or {}).get("media", {}).get("metadata", {})
        if normalize(meta.get("title", "")) != target_title:
            continue
        if not target_author_last:
            return True
        authors = meta.get("authors") or []
        if any(target_author_last in normalize(a.get("name", "")) for a in authors):
            return True
    return False


# ---------- live download health (qBittorrent / SABnzbd) ----------
# Family members can't see qbit/sab directly, so we surface real progress here
# instead of just LazyLibrarian's coarse Wanted/Snatched/Open status.

_qbit_cookies = None   # full cookie jar - qbit's session cookie name is port-specific
                       # (eg QBT_SID_8080), not a fixed "SID", so keep whatever it sets
_qbit_rid = 0          # sync/maindata is a diff API keyed off this - 0 forces a full snapshot
_qbit_torrents_cache = {}  # hash -> torrent dict, built up from the initial full sync + deltas


def _qbit_login():
    global _qbit_cookies, _qbit_rid, _qbit_torrents_cache
    try:
        r = requests.post(f"{cfg.QBIT_URL}/api/v2/auth/login",
                          data={"username": cfg.QBIT_USER, "password": cfg.QBIT_PASS}, timeout=10)
        _qbit_cookies = r.cookies if len(r.cookies) else None
        if not _qbit_cookies:
            logger.warning(f"qbit login returned no session cookie (status {r.status_code}): {r.text[:100]}")
        else:
            # new session - our cached rid/torrents may be stale relative to it, start fresh
            _qbit_rid = 0
            _qbit_torrents_cache = {}
    except Exception as e:
        logger.warning(f"qbit login failed: {e}")
        _qbit_cookies = None


def _qbit_sync():
    """Pull only what changed since our last rid (qbit's own WebUI uses this same
    endpoint for its live view) instead of re-fetching the full torrent list every
    poll - cheap enough to call often without adding real load."""
    global _qbit_cookies, _qbit_rid, _qbit_torrents_cache
    if not _qbit_cookies:
        _qbit_login()
    if not _qbit_cookies:
        return {}
    try:
        r = requests.get(f"{cfg.QBIT_URL}/api/v2/sync/maindata", cookies=_qbit_cookies,
                         params={"rid": _qbit_rid}, timeout=10)
        if r.status_code == 403:
            _qbit_login()
            if not _qbit_cookies:
                return {}
            r = requests.get(f"{cfg.QBIT_URL}/api/v2/sync/maindata", cookies=_qbit_cookies,
                             params={"rid": _qbit_rid}, timeout=10)
        data = r.json()
    except Exception as e:
        logger.warning(f"qbit sync fetch failed: {e}")
        return _qbit_torrents_cache

    _qbit_rid = data.get("rid", _qbit_rid)
    if data.get("full_update"):
        _qbit_torrents_cache = data.get("torrents", {})
    else:
        for h, fields in data.get("torrents", {}).items():
            _qbit_torrents_cache.setdefault(h, {}).update(fields)
        for h in data.get("torrents_removed", []):
            _qbit_torrents_cache.pop(h, None)
    return _qbit_torrents_cache


def _sab_queue_and_history():
    if not cfg.SAB_API_KEY:
        return [], []
    try:
        q = requests.get(f"{cfg.SAB_URL}/api", params={"mode": "queue", "apikey": cfg.SAB_API_KEY, "output": "json"}, timeout=10).json()
        h = requests.get(f"{cfg.SAB_URL}/api", params={"mode": "history", "apikey": cfg.SAB_API_KEY, "output": "json", "limit": 20}, timeout=10).json()
        return q.get("queue", {}).get("slots", []), h.get("history", {}).get("slots", [])
    except Exception as e:
        logger.warning(f"sab fetch failed: {e}")
        return [], []


def _word_overlap(target_words, name):
    name_words = set(normalize(name).split())
    if not name_words or not target_words:
        return 0
    return len(target_words & name_words) / len(target_words)


def match_download_health(title, author, book_type, qbit_torrents, sab_queue, sab_history):
    """Match one book's title against already-fetched qbit/sab snapshots - the
    snapshots are fetched once per poll cycle and shared across every request,
    not re-fetched per book.

    Title-only matching isn't enough: an ebook and audiobook of the same title
    can both be present, and an unrelated audiobook grab was once mistakenly
    reported as progress for an ebook request. qbit's category is useless for
    telling them apart - LL sends every torrent under the single "audiobooks"
    category regardless of actual content (confirmed against a real ~900KB
    epub landing there next to a real ~667MB audiobook mp3). Size is the
    actual signal: ebook files are single-digit MB at most, audiobooks are
    the better part of a GB. 30MB is comfortably between the two."""
    target_words = set(normalize(title).split())
    if not target_words:
        return None

    EBOOK_MAX_BYTES = 30_000_000

    best = None
    best_score = 0.5  # require at least half the title's words to match

    for t in qbit_torrents.values():
        size = t.get("size") or 0
        if book_type == "ebook" and size > EBOOK_MAX_BYTES:
            continue
        if book_type == "audiobook" and size <= EBOOK_MAX_BYTES:
            continue
        score = _word_overlap(target_words, t.get("name", ""))
        if score > best_score:
            best_score = score
            progress = round((t.get("progress") or 0) * 100, 1)
            state = t.get("state", "")
            speed = t.get("dlspeed") or 0
            if state in ("error", "missingFiles"):
                health = "broken"
            elif progress >= 100:
                health = "done"
            elif state in ("stalledDL", "metaDL") and speed == 0:
                health = "stalled"
            elif speed > 0 and speed < 200_000:  # under ~200KB/s
                health = "slow"
            elif speed >= 200_000:
                health = "downloading"
            else:
                health = "stalled"
            best = {"progress": progress, "health": health, "source": "qbit"}

    if best:
        return best

    for slot in sab_queue:
        score = _word_overlap(target_words, slot.get("filename", ""))
        if score > best_score:
            best_score = score
            progress = float(slot.get("percentage") or 0)
            status = (slot.get("status") or "").lower()
            timeleft = slot.get("timeleft", "")
            if status == "paused":
                health = "stalled"
            elif status in ("downloading", "queued") and timeleft and timeleft != "0:00:00":
                health = "downloading"
            else:
                health = "slow"
            best = {"progress": progress, "health": health, "source": "sab"}
    if best:
        return best

    for slot in sab_history:
        score = _word_overlap(target_words, slot.get("name", ""))
        if score > best_score:
            best_score = score
            status = (slot.get("status") or "").lower()
            if status == "failed":
                best = {"progress": 0, "health": "broken", "source": "sab-history"}
            elif status == "completed":
                best = {"progress": 100, "health": "done", "source": "sab-history"}

    return best
    return None


# ---------- Audible (primary audiobook source) ----------

def audible_search(keywords=None, title=None, author=None, num_results=24):
    params = {
        "num_results": num_results,
        "response_groups": "product_desc,media,product_extended_attrs",
        "marketplace": "US",
    }
    if keywords:
        params["keywords"] = keywords
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    r = requests.get(AUDIBLE_API, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("products", [])


def audible_to_result(p, existing_requests):
    images = p.get("product_images", {})
    cover = images.get("500") or images.get("1000") or images.get("300")
    authors = p.get("authors", [])
    author_name = authors[0]["name"] if authors else "Unknown"
    narrators = p.get("narrators", [])
    narrator_name = narrators[0]["name"] if narrators else None
    pub = p.get("publication_datetime", "") or ""
    release_date = pub.split("T")[0] if pub else "0000"
    asin = p.get("asin")

    already_have = abs_already_have(p.get("title", ""), author_name, "audiobook")
    req_status = existing_requests.get(asin)
    is_preorder = release_date != "0000" and release_date > datetime.utcnow().strftime("%Y-%m-%d")

    return {
        "source_id": asin,
        "authorid": None,
        "language": (p.get("language") or "").strip().lower(),
        "title": p.get("title", ""),
        "subtitle": p.get("subtitle"),
        "author": author_name,
        "narrator": narrator_name,
        "cover": cover,
        "release_date": release_date,
        "already_have": already_have,
        "request_status": req_status,
        "is_preorder": is_preorder,
    }


# ---------- ebook catalog search (backend-provided) ----------
# Was GoodReads-via-LL only; now whichever backend is active shapes its own
# catalog results into a common dict via backend.search_ebook() - see
# backends.py. resolve_goodreads_bookid also moved there (LLBackend._resolve_
# goodreads_bookid) - it was purely an LL/GoodReads artifact for bridging an
# Audible-sourced title to something LL's addBook can import; Shelfarr needs
# no equivalent bridge step.

def catalog_result_to_dict(result, existing_requests):
    d = result.as_dict()
    d["already_have"] = abs_already_have(d["title"], d["author"], "ebook")
    d["request_status"] = existing_requests.get(d["source_id"])
    return d


# ---------- cover image proxy/cache ----------
# Cover images (Audible, NYT, LazyLibrarian's own sources) were previously
# hotlinked straight from origin on every page load - re-fetching dozens of
# images from slow/rate-limited third-party CDNs on every discover refresh.
# This proxies + caches them to disk once, then serves cached copies with a
# long-lived Cache-Control so repeat loads hit neither the origin nor even
# this server (browser cache) after the first fetch.

os.makedirs(COVER_CACHE_DIR, exist_ok=True)
COVER_CACHE_TTL_HEADER = "public, max-age=2592000, immutable"  # 30 days


def _cover_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _is_safe_cover_url(url: str) -> bool:
    """Basic SSRF guard - this proxy fetches whatever URL the frontend hands
    it, so block anything that isn't a plain public http(s) host."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


@app.get("/api/cover")
def cover_proxy(u: str):
    key = _cover_cache_key(u)
    data_path = os.path.join(COVER_CACHE_DIR, f"{key}.bin")
    ctype_path = os.path.join(COVER_CACHE_DIR, f"{key}.ctype")

    if os.path.exists(data_path) and os.path.exists(ctype_path):
        with open(ctype_path, "r") as f:
            ctype = f.read().strip()
        with open(data_path, "rb") as f:
            return Response(content=f.read(), media_type=ctype, headers={"Cache-Control": COVER_CACHE_TTL_HEADER})

    if not _is_safe_cover_url(u):
        raise HTTPException(status_code=400, detail="Unsupported cover URL")

    try:
        r = requests.get(u, timeout=10, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        content = r.content
        if len(content) > 10 * 1024 * 1024:  # 10MB - a cover image is never legitimately this big
            raise HTTPException(status_code=502, detail="Cover image too large")
        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("cover fetch failed for %s: %s", u, e)
        raise HTTPException(status_code=502, detail="Failed to fetch cover")

    with open(data_path, "wb") as f:
        f.write(content)
    with open(ctype_path, "w") as f:
        f.write(ctype)

    return Response(content=content, media_type=ctype, headers={"Cache-Control": COVER_CACHE_TTL_HEADER})


# ---------- schema ----------

class RequestIn(BaseModel):
    requester: str
    book_type: str  # 'ebook' or 'audiobook'
    source_id: str
    authorid: Optional[str] = None
    title: str
    author: str
    cover: Optional[str] = None
    release_date: Optional[str] = None
    # True for ebook cards whose source_id isn't already a GoodReads/LL
    # bookid (e.g. NYT discovery results, which only give us an ISBN) - tells
    # create_request to resolve by title/author instead of trusting source_id
    needs_resolution: bool = False


@app.on_event("startup")
def startup():
    init_local_db()
    init_settings_db()
    t = threading.Thread(target=status_poll_loop, daemon=True)
    t.start()


@app.get("/api/users")
def list_users():
    return get_users()


# Audible/GoodReads return editions in every language for a keyword search, and
# there is no working server-side filter (language=english is ignored; locale=
# returns nothing). So we filter here. "unknown" is always kept - a lot of
# GoodReads records have no booklang and dropping them hides good results.
LANG_ALIASES = {
    "english": {"english", "eng", "en", "en-us", "en-gb", "en_us", "en_gb"},
    "spanish": {"spanish", "spa", "es", "espanol", "español"},
    "german":  {"german", "ger", "deu", "de"},
    "french":  {"french", "fre", "fra", "fr"},
    "italian": {"italian", "ita", "it"},
    "japanese": {"japanese", "jpn", "ja"},
}

def matches_language(value, wanted):
    """wanted='all' passes everything; unknown/blank always passes."""
    if wanted in ("", "all"):
        return True
    v = (value or "").strip().lower()
    if not v or v in ("unknown", "none", "null"):
        return True
    return v in LANG_ALIASES.get(wanted, {wanted})


@app.get("/api/languages")
def languages():
    """Selector options for the UI."""
    return [{"value": "all", "label": "All languages"}] + [
        {"value": k, "label": k.capitalize()} for k in sorted(LANG_ALIASES)
    ]


@app.get("/api/search")
def search(q: str, book_type: str = "audiobook", language: str = "english"):
    if not q.strip():
        return []

    conn_local = get_local_db()
    existing_requests = {
        r["source_id"]: r["status"]
        for r in conn_local.execute("SELECT source_id, status FROM requests WHERE book_type=?", (book_type,))
    }
    conn_local.close()

    if book_type == "audiobook":
        try:
            products = audible_search(keywords=q)
        except Exception as e:
            logger.warning(f"Audible search failed: {e}")
            products = []
        results = [audible_to_result(p, existing_requests) for p in products]
    else:
        active_backend = get_active_backend()
        try:
            raw_results = active_backend.search_ebook(q)
        except Exception as e:
            logger.warning(f"ebook search failed ({active_backend.name}): {e}")
            raw_results = []
        results = [catalog_result_to_dict(r, existing_requests) for r in raw_results]
        seen = set()
        deduped = []
        for r in results:
            if r["source_id"] in seen:
                continue
            seen.add(r["source_id"])
            deduped.append(r)
        results = deduped

    wanted = (language or "english").strip().lower()
    before = len(results)
    results = [r for r in results if matches_language(r.get("language"), wanted)]
    if before != len(results):
        logger.info(f"language filter '{wanted}': {before} -> {len(results)} results for {q!r}")

    def sort_key(r):
        d = r["release_date"] or "0000"
        return d if re.match(r'^\d{4}', d or '') else "0000"

    results.sort(key=sort_key, reverse=True)
    return results


# Audible's top-level browse categories (US marketplace) - static because this
# taxonomy barely changes, not worth a live API call on every page load.
AUDIBLE_GENRES = [
    ("18571910011", "Arts & Entertainment"),
    ("18571951011", "Biographies & Memoirs"),
    ("18572029011", "Business & Careers"),
    ("18572091011", "Children's Audiobooks"),
    ("24427740011", "Comedy & Humor"),
    ("18573211011", "Computers & Technology"),
    ("18573267011", "Education & Learning"),
    ("18573518011", "History"),
    ("18574426011", "Literature & Fiction"),
    ("18574597011", "Mystery, Thriller & Suspense"),
    ("18574784011", "Relationships, Parenting & Personal Development"),
    ("18574839011", "Religion & Spirituality"),
    ("18580518011", "Romance"),
    ("18580540011", "Science & Engineering"),
    ("18580606011", "Science Fiction & Fantasy"),
    ("18580648011", "Sports & Outdoors"),
    ("18580715011", "Teen & Young Adult"),
    ("18581095011", "Travel & Tourism"),
]


@app.get("/api/genres")
def get_genres():
    return [{"id": gid, "name": name} for gid, name in AUDIBLE_GENRES]


@app.get("/api/new-releases")
def new_releases(category_id: str = ""):
    conn_local = get_local_db()
    try:
        existing_requests = {
            r["source_id"]: r["status"]
            for r in conn_local.execute("SELECT source_id, status FROM requests WHERE book_type='audiobook'")
        }
    finally:
        conn_local.close()

    # Popular categories have a deep backlog of far-future pre-orders - sorted
    # by -ReleaseDate, even 200 results in can be entirely months away, so
    # filtering to "recently released" isn't practical with that sort. Sorting
    # by Relevance instead gives a genuine mix of already-out and upcoming
    # titles in a single page, which we then split into two buckets.
    params = {
        "num_results": 50,
        "response_groups": "product_desc,media,contributors",
        "marketplace": "US",
        "products_sort_by": "Relevance",
    }
    if category_id:
        params["category_id"] = category_id
    try:
        r = requests.get(AUDIBLE_API, params=params, timeout=15)
        r.raise_for_status()
        products = r.json().get("products", [])
    except Exception as e:
        logger.warning(f"Audible new-releases fetch failed: {e}")
        products = []

    results = [audible_to_result(p, existing_requests) for p in products]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    month_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    released = sorted(
        (r for r in results if r["release_date"] != "0000" and month_ago <= r["release_date"] <= today),
        key=lambda r: r["release_date"], reverse=True,
    )
    preorder = sorted(
        (r for r in results if r["release_date"] > today),
        key=lambda r: r["release_date"],
    )
    return {"new": released[:18], "preorder": preorder[:18]}


# ---------- ebook discovery (NYT bestseller lists) ----------
# Unlike Audible, everything on a bestseller list is by definition already
# published - no pre-order pollution, no date filtering needed. Lists refresh
# weekly on NYT's end, so a cheap in-memory cache avoids hitting their (free,
# 1000/day) quota on every page load.

NYT_API = "https://api.nytimes.com/svc/books/v3/lists/overview.json"
_nyt_cache = {"data": None, "fetched_at": 0}
NYT_CACHE_TTL = 3600

# NYT splits its lists into print/ebook ones and 4 dedicated audio ones - each
# side only makes sense for the matching book_type (ISBNs on the audio lists
# are for the print edition, not the actual audiobook).
NYT_AUDIO_LISTS = {
    "Audio Advice, How-To & Misc", "Audio Children's", "Audio Fiction", "Audio Nonfiction",
}


def _nyt_overview():
    now = time.time()
    if _nyt_cache["data"] and now - _nyt_cache["fetched_at"] < NYT_CACHE_TTL:
        return _nyt_cache["data"]
    r = requests.get(NYT_API, params={"api-key": cfg.NYT_API_KEY}, timeout=15)
    r.raise_for_status()
    lists = r.json().get("results", {}).get("lists", [])
    _nyt_cache.update(data=lists, fetched_at=now)
    return lists


def nyt_book_to_result(b, existing_requests, book_type):
    isbn = b.get("primary_isbn13", "")
    already_have = abs_already_have(b.get("title", ""), b.get("author", ""), book_type)
    return {
        "source_id": isbn,
        "authorid": None,
        "title": (b.get("title") or "").title(),
        "subtitle": b.get("description"),
        "author": b.get("author", ""),
        "narrator": None,
        "cover": b.get("book_image"),
        "release_date": None,
        "already_have": already_have,
        "request_status": existing_requests.get(isbn),
        "needs_resolution": True,
    }


def _nyt_existing(book_type):
    conn_local = get_local_db()
    try:
        return {
            r["source_id"]: r["status"]
            for r in conn_local.execute("SELECT source_id, status FROM requests WHERE book_type=?", (book_type,))
        }
    finally:
        conn_local.close()


@app.get("/api/ebook-genres")
def get_ebook_genres():
    try:
        lists = _nyt_overview()
    except Exception as e:
        logger.warning(f"NYT lists fetch failed: {e}")
        return []
    return [l["list_name"] for l in lists if l["list_name"] not in NYT_AUDIO_LISTS]


@app.get("/api/ebook-discover")
def ebook_discover(list_name: str = ""):
    existing_requests = _nyt_existing("ebook")
    try:
        lists = _nyt_overview()
    except Exception as e:
        logger.warning(f"NYT lists fetch failed: {e}")
        return []

    lists = [l for l in lists if l["list_name"] not in NYT_AUDIO_LISTS]
    if list_name:
        lists = [l for l in lists if l["list_name"] == list_name]
    else:
        lists = lists[:1]  # default view: just the flagship Combined Fiction list

    books = [b for l in lists for b in l.get("books", [])]
    return [nyt_book_to_result(b, existing_requests, "ebook") for b in books]


@app.get("/api/audiobook-bestsellers")
def audiobook_bestsellers():
    """NYT's 4 audio lists, combined - unlike the Audible-sourced New/Pre-order
    rows, everything here is guaranteed already published and real, so it's a
    clean complement rather than a replacement."""
    existing_requests = _nyt_existing("audiobook")
    try:
        lists = _nyt_overview()
    except Exception as e:
        logger.warning(f"NYT lists fetch failed: {e}")
        return []

    lists = [l for l in lists if l["list_name"] in NYT_AUDIO_LISTS]
    seen = set()
    books = []
    for l in lists:
        for b in l.get("books", []):
            isbn = b.get("primary_isbn13", "")
            if isbn and isbn not in seen:
                seen.add(isbn)
                books.append(b)
    return [nyt_book_to_result(b, existing_requests, "audiobook") for b in books[:18]]


@app.post("/api/request")
def create_request(req: RequestIn):
    if req.requester not in get_users() and req.requester != EVERYONE_TAG:
        raise HTTPException(400, "Unknown requester")
    if req.book_type not in ("ebook", "audiobook"):
        raise HTTPException(400, "book_type must be ebook or audiobook")

    now = datetime.utcnow().isoformat()

    # Everything backend-specific - resolving a catalog id, adding the book,
    # marking it wanted (or holding a preorder), locking/dual-format
    # clearing where the backend needs that, triggering a search - happens
    # inside backend.submit(). See backends.py: LLBackend does exactly what
    # this function used to do inline; ShelfarrBackend's model needs none
    # of the locking/drift-correction machinery LL's does.
    active_backend = get_active_backend()
    try:
        result = active_backend.submit(
            title=req.title,
            author=req.author,
            book_type=req.book_type,
            source_id=req.source_id,
            authorid=req.authorid,
            release_date=req.release_date,
            needs_resolution=req.needs_resolution,
        )
    except Exception as e:
        # The raw exception (connection refused, read timeout, etc) is
        # genuinely alarming to a non-technical household member and reads
        # as "the whole system is broken" - logged here for whoever's
        # actually troubleshooting, but what the requester sees stays calm
        # and actionable.
        logger.error(f"{active_backend.name} submit failed for {req.title!r}: {e}")
        raise HTTPException(
            503,
            f"{active_backend.name.capitalize()} is busy or briefly unreachable - "
            "your request wasn't saved. Please try again in a minute.",
        )

    conn = get_local_db()
    try:
        conn.execute("""
            INSERT INTO requests (requester, book_type, source_id, ll_bookid, authorid, title, author, cover_url, release_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, book_type) DO UPDATE SET
                requester=excluded.requester, updated_at=excluded.updated_at
        """, (req.requester, req.book_type, req.source_id, result.backend_ref, req.authorid, req.title, req.author,
              req.cover, req.release_date, result.status, now, now))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "resolved": result.resolved}


@app.get("/api/my-requests")
def my_requests(requester: str):
    conn = get_local_db()
    try:
        rows = conn.execute(
            "SELECT * FROM requests WHERE requester=? ORDER BY created_at DESC", (requester,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/all-requests")
def all_requests():
    conn = get_local_db()
    try:
        rows = conn.execute("SELECT * FROM requests ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


KEEP_SEEDING_COUNT = 20
ARCHIVE_MIN_AGE_SECONDS = 3600  # extra safety margin beyond LL's 10-min postprocess cycle,
                                # so we never race a completed download that hasn't been
                                # copied into the real library yet

# Book/audiobook trackers routinely sit at zero seeders for a day or two and then
# pick back up - a single zero-seed snapshot is not evidence a torrent is dead.
# Only treat one as abandoned after it's been sitting at essentially 0% progress
# for a long stretch, and only if it's *still* zero seeds right now too.
STALE_MIN_AGE_SECONDS = 5 * 24 * 3600  # 5 days
STALE_PROGRESS_CEILING = 0.01

DONE_STATES = {"stalledUP", "uploading", "queuedUP", "forcedUP", "pausedUP"}


def _qbit_delete(hashes, delete_files=True):
    global _qbit_cookies
    if not _qbit_cookies:
        _qbit_login()
    if not _qbit_cookies:
        return False
    try:
        requests.post(f"{cfg.QBIT_URL}/api/v2/torrents/delete", cookies=_qbit_cookies,
                      data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()},
                      timeout=10)
        return True
    except Exception as e:
        logger.warning(f"qbit delete failed: {e}")
        return False


# Both backends' download-client categories - LL's downloads use "audiobooks"
# (unchanged), Shelfarr's use "shelfarr" (see qbt_audiobook_queue_triage in
# homelab-memory for the matching Unraid-side cleanup cron, which handles
# early-stage priority/purge on a faster cadence than these two - this pair
# handles the later "fully done, still seeding" and "long-dead" cases).
BOOK_CATEGORIES = {"audiobooks", "shelfarr"}


def archive_old_downloads(torrents):
    """The backend copies finished downloads into the real library, but
    leaves the original seeding in qBittorrent forever. Once there are more
    than KEEP_SEEDING_COUNT finished book torrents, remove the oldest ones
    from qBittorrent (their data is already safely duplicated in the
    library) so the client doesn't accumulate indefinitely."""
    now = time.time()
    done = [
        (h, t) for h, t in torrents.items()
        if t.get("category") in BOOK_CATEGORIES
        and (t.get("progress") or 0) >= 1.0
        and t.get("state") in DONE_STATES
    ]
    if len(done) <= KEEP_SEEDING_COUNT:
        return

    done.sort(key=lambda ht: ht[1].get("completion_on") or 0, reverse=True)
    to_remove = [
        (h, t) for h, t in done[KEEP_SEEDING_COUNT:]
        if now - (t.get("completion_on") or now) > ARCHIVE_MIN_AGE_SECONDS
    ]
    if to_remove and _qbit_delete([h for h, t in to_remove]):
        for h, t in to_remove:
            logger.info(f"Archived (removed from qbit) completed download: {t.get('name')}")


def cleanup_stale_torrents(torrents):
    """Find book torrents that have been sitting at ~0% for STALE_MIN_AGE_SECONDS
    with no seeders right now either - genuinely abandoned, not just a temporary
    dry spell. Remove the dead torrent and reset the matching request's status
    back to Wanted so the backend's normal search cycle tries again (possibly
    finding a different, live release) instead of leaving it stuck forever."""
    now = time.time()
    stale = [
        (h, t) for h, t in torrents.items()
        if t.get("category") in BOOK_CATEGORIES
        and (t.get("progress") or 0) < STALE_PROGRESS_CEILING
        and (t.get("num_seeds") or 0) == 0
        and now - (t.get("added_on") or now) > STALE_MIN_AGE_SECONDS
    ]
    if not stale:
        return

    conn_local = get_local_db()
    try:
        pending = conn_local.execute(
            "SELECT id, ll_bookid, book_type, title, author FROM requests WHERE status IN ('wanted', 'snatched')"
        ).fetchall()
    finally:
        conn_local.close()

    active_backend = get_active_backend()
    removed_hashes = []
    for h, t in stale:
        target_words = set(normalize(t.get("name", "")).split())
        for r in pending:
            score = _word_overlap(target_words, r["title"])
            if score > 0.5 and r["ll_bookid"]:
                try:
                    active_backend.reset_wanted(r["ll_bookid"], r["book_type"])
                    logger.info(f"Stale torrent for '{r['title']}' (0 seeds, {STALE_MIN_AGE_SECONDS // 86400}+ days at ~0%) - reset for retry")
                except Exception as e:
                    logger.warning(f"Failed to reset stale request {r['id']}: {e}")
                break
        removed_hashes.append(h)

    if removed_hashes and _qbit_delete(removed_hashes):
        for h, t in stale:
            logger.info(f"Removed dead torrent (0 seeds, stale): {t.get('name')}")


def status_poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            logger.warning(f"status poll error: {e}")
        try:
            torrents = _qbit_sync()
            archive_old_downloads(torrents)
            cleanup_stale_torrents(torrents)
        except Exception as e:
            logger.warning(f"archive/cleanup check error: {e}")
        time.sleep(90)  # sync/maindata is a cheap delta fetch, so this can run closer to real-time


def poll_once():
    conn_local = get_local_db()
    try:
        rows = conn_local.execute(
            "SELECT id, ll_bookid, book_type, status, title, author, release_date FROM requests "
            "WHERE status NOT IN ('downloaded', 'unresolved')"
        ).fetchall()
    finally:
        conn_local.close()
    if not rows:
        return

    active_backend = get_active_backend()

    # Backend-specific maintenance (LL: promote preorders held at Skipped
    # once their release date arrives, so its normal search cron picks
    # them up starting exactly then; Shelfarr: no-op, its own recurring
    # jobs already handle this).
    try:
        active_backend.poll_maintenance(rows)
    except Exception as e:
        logger.warning(f"backend poll_maintenance failed: {e}")

    status_updates = []
    health_updates = []
    now = datetime.utcnow().isoformat()

    # fetch qbit/sab state exactly once per cycle and reuse it for every pending
    # request below, instead of re-fetching per book
    needs_health = any(r["status"] in ("wanted", "snatched") for r in rows)
    if needs_health:
        qbit_torrents = _qbit_sync()
        sab_queue, sab_history = _sab_queue_and_history()
    else:
        qbit_torrents, sab_queue, sab_history = {}, [], []

    for r in rows:
        # 1. status against the backend's own catalog (Wanted/Snatched/Downloaded)
        if r["ll_bookid"]:
            try:
                new_status = active_backend.get_status(r["ll_bookid"], r["book_type"])
            except Exception as e:
                logger.warning(f"poll: {active_backend.name} lookup failed for {r['ll_bookid']}: {e}")
                new_status = None
            if new_status and new_status != r["status"]:
                status_updates.append((new_status, r["id"]))

        # 2. real download progress from qbit/sab - only worth checking once
        # something's actually been sent to a downloader (wanted or snatched)
        if r["status"] in ("wanted", "snatched"):
            try:
                health = match_download_health(r["title"], r["author"], r["book_type"], qbit_torrents, sab_queue, sab_history)
            except Exception as e:
                logger.warning(f"health check failed for {r['title']}: {e}")
                health = None
            if health:
                health_updates.append((health["progress"], health["health"], now, r["id"]))
            else:
                health_updates.append((0, "sending", now, r["id"]))

    if status_updates or health_updates:
        conn_local = get_local_db()
        try:
            for new_status, rid in status_updates:
                conn_local.execute(
                    "UPDATE requests SET status=?, updated_at=? WHERE id=?", (new_status, now, rid)
                )
            for progress, health, checked_at, rid in health_updates:
                conn_local.execute(
                    "UPDATE requests SET progress_pct=?, download_health=?, health_checked_at=? WHERE id=?",
                    (progress, health, checked_at, rid)
                )
            conn_local.commit()
            if status_updates:
                logger.info(f"status poll: updated {len(status_updates)} requests")
        finally:
            conn_local.close()


# ---------- settings / config screen ----------
# No auth by design - this is meant for a trusted LAN/Tailscale network, not
# the open internet. See README for the disclaimer this ships with.

class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/config")
def config_page():
    from fastapi.responses import FileResponse
    return FileResponse("static/config.html")


@app.get("/api/settings")
def api_get_settings():
    return get_all_settings_masked()


@app.post("/api/settings")
def api_save_settings(body: SettingsIn):
    for key, value in body.values.items():
        if key == "USERS":
            set_users([u for u in value.split(",")])
            continue
        if not any(k == key for k, _label, _secret, _default in SETTING_FIELDS):
            continue
        # secrets render masked ("••••••••") on load - never write that back
        # over a real stored value if the admin didn't actually change it
        if value == "••••••••":
            continue
        set_setting(key, value)
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
