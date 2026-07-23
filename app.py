import os
import re
import sqlite3
import time
import threading
import logging
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from settings import (
    cfg, init_settings_db, get_users, set_users, set_setting,
    get_all_settings_masked, SETTING_FIELDS,
)
from abb_bridge import app as abb_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prettylazylibwrapper")

LL_DB_PATH = os.environ.get("LL_DB_PATH", "/ll-db/lazylibrarian.db")  # mounted read-write
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "/data/prettylazylibwrapper.db")

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


# ---------- LazyLibrarian's db (shared, read-mostly) ----------

def get_ll_db_readonly():
    uri = f"file:{LL_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_ll_db_readwrite():
    # used only to flip Status/AudioStatus to Wanted after LL has created the book row -
    # LL's own API has no apikey-only "mark wanted" command, and the web endpoint that
    # does this needs a logged-in session, so we do the same write LL itself would do.
    conn = sqlite3.connect(LL_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ll_api(cmd, **params):
    p = {"apikey": cfg.LL_API_KEY, "cmd": cmd}
    p.update(params)
    r = requests.get(f"{cfg.LL_URL}/api", params=p, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return r.text


def normalize(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def ll_already_have(title, author, book_type):
    """Fuzzy title/author match against LL's own library - covers the audiobook
    case where we search Audible but LL's records are keyed by GoodReads id."""
    try:
        conn = get_ll_db_readonly()
    except Exception as e:
        logger.warning(f"Could not open LL db: {e}")
        return None
    status_col = "AudioStatus" if book_type == "audiobook" else "Status"
    try:
        rows = conn.execute(
            f"SELECT b.BookName, b.{status_col} as st FROM books b, authors a "
            "WHERE b.AuthorID = a.AuthorID AND a.AuthorName LIKE ?",
            (f"%{author.split()[-1]}%",)
        ).fetchall()
    finally:
        conn.close()
    target = normalize(title)
    for r in rows:
        if normalize(r["BookName"]) == target:
            return r["st"]
    return None


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
        "response_groups": "product_desc,media",
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

    already_have = ll_already_have(p.get("title", ""), author_name, "audiobook") in ("Open", "Have")
    req_status = existing_requests.get(asin)

    return {
        "source_id": asin,
        "authorid": None,
        "title": p.get("title", ""),
        "subtitle": p.get("subtitle"),
        "author": author_name,
        "narrator": narrator_name,
        "cover": cover,
        "release_date": release_date,
        "already_have": already_have,
        "request_status": req_status,
    }


# ---------- GoodReads (ebook source, and audiobook id-resolution bridge) ----------

def goodreads_to_result(item, existing_requests):
    bookid = item.get("bookid")
    already_have = ll_already_have(item.get("bookname", ""), item.get("authorname", ""), "ebook") in ("Open", "Have")
    cover = item.get("bookimg")
    if cover and not cover.startswith("http"):
        cover = None
    return {
        "source_id": bookid,
        "authorid": item.get("authorid"),
        "title": item.get("bookname", ""),
        "subtitle": None,
        "author": item.get("authorname", ""),
        "narrator": None,
        "cover": cover,
        "release_date": item.get("bookdate") or "0000",
        "already_have": already_have,
        "request_status": existing_requests.get(bookid),
    }


def resolve_goodreads_bookid(title, author):
    """Given a clean title/author (from an Audible result), find the matching
    GoodReads bookid LL's backend can actually import - LL's addBook/addAuthorID
    only understand GoodReads ids, not Audible ASINs."""
    raw = ll_api("findBook", name=f"{title} {author}")
    if not isinstance(raw, list) or not raw:
        return None, None
    best = max(raw, key=lambda r: r.get("highest_fuzz", 0))
    if best.get("highest_fuzz", 0) < 55:
        return None, None
    return best.get("bookid"), best.get("authorid")


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


@app.on_event("startup")
def startup():
    init_local_db()
    init_settings_db()
    t = threading.Thread(target=status_poll_loop, daemon=True)
    t.start()


@app.get("/api/users")
def list_users():
    return get_users()


@app.get("/api/search")
def search(q: str, book_type: str = "audiobook"):
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
        raw = ll_api("findBook", name=q)
        results = [goodreads_to_result(i, existing_requests) for i in raw] if isinstance(raw, list) else []
        seen = set()
        deduped = []
        for r in results:
            if r["source_id"] in seen:
                continue
            seen.add(r["source_id"])
            deduped.append(r)
        results = deduped

    def sort_key(r):
        d = r["release_date"] or "0000"
        return d if re.match(r'^\d{4}', d or '') else "0000"

    results.sort(key=sort_key, reverse=True)
    return results


@app.post("/api/request")
def create_request(req: RequestIn):
    if req.requester not in get_users() and req.requester != EVERYONE_TAG:
        raise HTTPException(400, "Unknown requester")
    if req.book_type not in ("ebook", "audiobook"):
        raise HTTPException(400, "book_type must be ebook or audiobook")

    now = datetime.utcnow().isoformat()

    if req.book_type == "audiobook":
        ll_bookid, ll_authorid = resolve_goodreads_bookid(req.title, req.author)
    else:
        ll_bookid, ll_authorid = req.source_id, req.authorid

    status = "submitted"
    if not ll_bookid:
        # record the request anyway so it's visible, but flag that LazyLibrarian
        # couldn't find a matching catalog entry to actually search/download against
        status = "unresolved"
    else:
        if ll_authorid:
            try:
                ll_api("addAuthorID", id=ll_authorid)
            except Exception as e:
                logger.warning(f"addAuthorID failed (continuing): {e}")
        try:
            ll_api("addBook", id=ll_bookid)
        except Exception as e:
            raise HTTPException(502, f"Failed to reach LazyLibrarian: {e}")

        status_col = "AudioStatus" if req.book_type == "audiobook" else "Status"
        other_type = "ebook" if req.book_type == "audiobook" else "audiobook"
        other_status_col = "Status" if status_col == "AudioStatus" else "AudioStatus"

        # LL defaults every newly-added book to Status=Skipped, AudioStatus=Wanted
        # regardless of which format was actually requested. Left alone, that stray
        # Wanted silently triggers a real search+download for a format nobody asked
        # for (seen twice: an ebook request pulled down the audiobook edition
        # instead). Only reset it if nobody else has a genuine active request for
        # that other format on this book.
        conn = get_local_db()
        try:
            other_active = conn.execute(
                "SELECT 1 FROM requests WHERE ll_bookid=? AND book_type=? AND status NOT IN ('unresolved')",
                (ll_bookid, other_type)
            ).fetchone()
        finally:
            conn.close()

        # addBook's import runs in a background thread on LL's side and can take a
        # variable amount of time, especially when the book already has a partial
        # record (eg. the other format already exists). Poll for the row to exist
        # before we try to mark it Wanted, rather than a fixed sleep.
        book_exists = False
        for _ in range(15):
            try:
                conn = get_ll_db_readonly()
                row = conn.execute("SELECT BookID FROM books WHERE BookID=?", (ll_bookid,)).fetchone()
                conn.close()
                if row:
                    book_exists = True
                    break
            except Exception as e:
                logger.warning(f"Polling for {ll_bookid} failed: {e}")
            time.sleep(1)

        def mark_wanted():
            conn = get_ll_db_readwrite()
            conn.execute(f"UPDATE books SET {status_col}=? WHERE BookID=?", ("Wanted", ll_bookid))
            if not other_active:
                conn.execute(f"UPDATE books SET {other_status_col}=? WHERE BookID=? AND {other_status_col}='Wanted'",
                             ("Skipped", ll_bookid))
            conn.commit()
            conn.close()

        if not book_exists:
            logger.warning(f"{ll_bookid} never appeared in LL's DB after addBook - marking wanted anyway in case it lands late")
        try:
            mark_wanted()
        except Exception as e:
            raise HTTPException(502, f"Failed to mark book wanted in LazyLibrarian: {e}")

        # LL's own background import can still be mid-flight and clobber the status
        # we just set once it finishes its own upsert. Re-check and re-apply a couple
        # of times over the next few seconds as a safety net against that race.
        for _ in range(3):
            time.sleep(2)
            try:
                conn = get_ll_db_readonly()
                row = conn.execute(f"SELECT {status_col} as st, {other_status_col} as ost FROM books WHERE BookID=?", (ll_bookid,)).fetchone()
                conn.close()
                if row and (row["st"] != "Wanted" or (not other_active and row["ost"] == "Wanted")):
                    logger.info(f"{ll_bookid} status drifted (st={row['st']!r} ost={row['ost']!r}), re-applying")
                    mark_wanted()
            except Exception as e:
                logger.warning(f"Post-check for {ll_bookid} failed: {e}")

        try:
            ll_api("searchBook", id=ll_bookid)
        except Exception as e:
            logger.warning(f"searchBook trigger failed (will pick up on next scheduled search): {e}")

    conn = get_local_db()
    try:
        conn.execute("""
            INSERT INTO requests (requester, book_type, source_id, ll_bookid, authorid, title, author, cover_url, release_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, book_type) DO UPDATE SET
                requester=excluded.requester, updated_at=excluded.updated_at
        """, (req.requester, req.book_type, req.source_id, ll_bookid, ll_authorid, req.title, req.author,
              req.cover, req.release_date, status, now, now))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "resolved": bool(ll_bookid)}


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


def archive_old_downloads(torrents):
    """LL copies finished downloads into the real library, but leaves the original
    seeding in qBittorrent forever. Once there are more than KEEP_SEEDING_COUNT
    finished book torrents, remove the oldest ones from qBittorrent (their data is
    already safely duplicated in the library) so the client doesn't accumulate
    indefinitely."""
    now = time.time()
    done = [
        (h, t) for h, t in torrents.items()
        if t.get("category") == "audiobooks"
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
    back to Wanted so LL's normal search cycle tries again (possibly finding a
    different, live release) instead of leaving it stuck forever."""
    now = time.time()
    stale = [
        (h, t) for h, t in torrents.items()
        if t.get("category") == "audiobooks"
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

    removed_hashes = []
    for h, t in stale:
        target_words = set(normalize(t.get("name", "")).split())
        for r in pending:
            score = _word_overlap(target_words, r["title"])
            if score > 0.5 and r["ll_bookid"]:
                try:
                    conn = get_ll_db_readwrite()
                    status_col = "AudioStatus" if r["book_type"] == "audiobook" else "Status"
                    conn.execute(f"UPDATE books SET {status_col}=? WHERE BookID=?", ("Wanted", r["ll_bookid"]))
                    conn.commit()
                    conn.close()
                    logger.info(f"Stale torrent for '{r['title']}' (0 seeds, {STALE_MIN_AGE_SECONDS // 86400}+ days at ~0%) - reset to Wanted for retry")
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
            "SELECT id, ll_bookid, book_type, status, title, author FROM requests "
            "WHERE status NOT IN ('downloaded', 'unresolved')"
        ).fetchall()
    finally:
        conn_local.close()
    if not rows:
        return

    try:
        conn_ll = get_ll_db_readonly()
    except Exception as e:
        logger.warning(f"poll: could not open LL db: {e}")
        conn_ll = None

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
        # 1. status against LL's own db (Wanted/Snatched/Open)
        if conn_ll and r["ll_bookid"]:
            try:
                row = conn_ll.execute(
                    "SELECT Status, AudioStatus FROM books WHERE BookID=?", (r["ll_bookid"],)
                ).fetchone()
            except Exception as e:
                logger.warning(f"poll: LL lookup failed for {r['ll_bookid']}: {e}")
                row = None
            if row:
                ll_status = row["AudioStatus"] if r["book_type"] == "audiobook" else row["Status"]
                new_status = {
                    "Wanted": "wanted",
                    "Snatched": "snatched",
                    "Open": "downloaded",
                    "Have": "downloaded",
                }.get(ll_status)
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

    if conn_ll:
        conn_ll.close()

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
