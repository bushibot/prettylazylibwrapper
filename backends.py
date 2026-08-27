"""Pluggable acquisition-backend layer.

app.py used to call LazyLibrarian's API (ll_api()) and read its SQLite DB
directly, ad-hoc, from ~13 call sites. This module gives that a real seam:
`Backend` is the interface app.py talks to; `LLBackend` is the existing
LazyLibrarian behavior moved here unchanged; `ShelfarrBackend` is the new
Shelfarr integration. Which one is active is a live setting (`BACKEND`),
so switching is a /config change, not a redeploy.

Hard rule carried over from the LL corruption incident
(lazylibrarian-db-corruption-20260818.md): writes only ever go through a
backend's own API, never a direct DB write. LLBackend still reads LL's DB
directly (an established, safe optimization - see get_ll_db_readonly's own
history), but every state change goes through LL's own queueBook/
unqueueBook/setBookLock/searchBook commands, exactly as before.
ShelfarrBackend has no DB access at all - REST API only - so this class of
bug isn't structurally possible there.

LazyLibrarian's status model is one row per book with two status columns
(Status/AudioStatus) that can drift out of sync with what was actually
requested - that's what the "drift correction" background thread exists to
fix. Shelfarr's model is one request per (work, book_type) from creation,
so that whole problem category doesn't exist there. Rather than force a
lowest-common-denominator interface, LL-specific correction machinery stays
inside LLBackend; ShelfarrBackend's equivalent hooks are genuinely no-ops.
"""
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger("prettylazylibwrapper.backends")


def _normalize(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


class NormalizedResult:
    """Common shape every backend's search_ebook() returns, regardless of
    the underlying catalog's own field names."""

    def __init__(self, source_id, title, author, authorid=None, language="",
                 subtitle=None, narrator=None, cover=None, release_date="0000",
                 needs_resolution=False):
        self.source_id = source_id
        self.authorid = authorid
        self.language = language
        self.title = title
        self.subtitle = subtitle
        self.author = author
        self.narrator = narrator
        self.cover = cover
        self.release_date = release_date or "0000"
        self.needs_resolution = needs_resolution

    def as_dict(self):
        return {
            "source_id": self.source_id,
            "authorid": self.authorid,
            "language": self.language,
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.author,
            "narrator": self.narrator,
            "cover": self.cover,
            "release_date": self.release_date,
            "needs_resolution": self.needs_resolution,
        }


class SubmitResult:
    """What create_request() needs back from submit(): the opaque backend
    reference to store in the local requests table (existing `ll_bookid`
    column, kept as-is regardless of backend to avoid a schema migration -
    it's just "backend_ref" now), whether it resolved to something the
    backend can actually search/download, and the initial local status."""

    def __init__(self, backend_ref, resolved, status="submitted"):
        self.backend_ref = backend_ref
        self.resolved = resolved
        self.status = status


class Backend:
    """Interface app.py codes against. Every method that used to be a
    direct ll_api()/DB call site in app.py should route through one of
    these instead."""

    name = "base"

    def search_ebook(self, query: str) -> list:
        """Return list[NormalizedResult] - ebook catalog search."""
        raise NotImplementedError

    def submit(self, *, title, author, book_type, source_id, authorid,
               release_date, needs_resolution) -> SubmitResult:
        """Everything create_request() needs done backend-side: resolve to
        a catalog entry if needed, add it, mark it wanted (or hold at a
        not-yet-released state), trigger a search. Backend-specific
        correction/locking behavior (LL's drift fix, dual-format clearing)
        lives entirely inside the implementation - callers don't need to
        know it happened."""
        raise NotImplementedError

    def get_status(self, backend_ref, book_type) -> "str | None":
        """Normalized status: 'wanted' | 'snatched' | 'downloaded' | None
        (None = no change / not found)."""
        raise NotImplementedError

    def retry_search(self, backend_ref, book_type) -> None:
        raise NotImplementedError

    def reset_wanted(self, backend_ref, book_type) -> None:
        """Used by cleanup_stale_torrents() after removing a dead torrent -
        put the request back in a state that'll be retried."""
        raise NotImplementedError

    def poll_maintenance(self, pending_rows) -> None:
        """Called once per poll_once() cycle with every locally-pending
        request row. LL uses this for preorder-promotion and drift
        correction against its own DB; backends with a simpler model (no
        drift possible) can no-op."""
        raise NotImplementedError

    def already_have(self, title, author, book_type) -> bool:
        """Deprecated per-backend duplicate check - see abs_already_have()
        in app.py, which replaced this with a check against Audiobookshelf
        directly (the actual source of truth for what's owned, independent
        of which backend is active). Kept only so LLBackend's existing
        fuzzy-match code has somewhere to live if ever needed standalone;
        app.py no longer calls this."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# LazyLibrarian
# ---------------------------------------------------------------------------

class LLBackend(Backend):
    name = "lazylibrarian"

    def __init__(self, cfg, ll_db_path):
        self.cfg = cfg
        self.ll_db_path = ll_db_path

    # -- transport --

    def _api(self, cmd, **params):
        """LL occasionally takes a while to answer mid-postprocess (a big
        download batch can tie it up for tens of seconds) - that's normal
        business, not an outage, so a lone connection/timeout blip gets a
        couple of quick retries before this actually gives up."""
        p = {"apikey": self.cfg.LL_API_KEY, "cmd": cmd}
        p.update(params)
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.get(f"{self.cfg.LL_URL}/api", params=p, timeout=30)
                r.raise_for_status()
                try:
                    return r.json()
                except ValueError:
                    return r.text
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise last_exc

    def _db_ro(self):
        uri = f"file:{self.ll_db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    # -- search --

    def search_ebook(self, query):
        raw = self._api("findBook", name=query)
        if not isinstance(raw, list):
            return []
        results = []
        seen = set()
        for item in raw:
            bookid = item.get("bookid")
            if bookid in seen:
                continue
            seen.add(bookid)
            cover = item.get("bookimg")
            if cover and not cover.startswith("http"):
                cover = None
            results.append(NormalizedResult(
                source_id=bookid,
                authorid=item.get("authorid"),
                language=(item.get("booklang") or "").strip().lower(),
                title=item.get("bookname", ""),
                author=item.get("authorname", ""),
                cover=cover,
                release_date=item.get("bookdate") or "0000",
            ))
        return results

    def _resolve_goodreads_bookid(self, title, author):
        """Given a clean title/author (e.g. from an Audible result), find
        the matching GoodReads bookid LL's backend can actually import -
        LL's addBook/addAuthorID only understand GoodReads ids, not
        Audible ASINs."""
        raw = self._api("findBook", name=f"{title} {author}")
        if not isinstance(raw, list) or not raw:
            return None, None
        best = max(raw, key=lambda r: r.get("highest_fuzz", 0))
        if best.get("highest_fuzz", 0) < 55:
            return None, None
        return best.get("bookid"), best.get("authorid")

    # -- submit --

    def submit(self, *, title, author, book_type, source_id, authorid,
               release_date, needs_resolution):
        if book_type == "audiobook" or needs_resolution:
            ll_bookid, ll_authorid = self._resolve_goodreads_bookid(title, author)
        else:
            ll_bookid, ll_authorid = source_id, authorid

        if not ll_bookid:
            return SubmitResult(backend_ref=None, resolved=False, status="unresolved")

        if ll_authorid:
            try:
                self._api("addAuthorID", id=ll_authorid)
            except Exception as e:
                logger.warning(f"addAuthorID failed (continuing): {e}")
        try:
            self._api("addBook", id=ll_bookid)
        except Exception as e:
            logger.error(f"addBook failed for LL book {ll_bookid}: {e}")
            raise

        status_col = "AudioStatus" if book_type == "audiobook" else "Status"
        other_type = "ebook" if book_type == "audiobook" else "audiobook"
        other_status_col = "Status" if status_col == "AudioStatus" else "AudioStatus"

        # addBook's import runs in a background thread on LL's side - poll
        # for the row to exist before marking it Wanted, rather than a
        # fixed sleep.
        book_exists = False
        for _ in range(15):
            try:
                conn = self._db_ro()
                row = conn.execute("SELECT BookID FROM books WHERE BookID=?", (ll_bookid,)).fetchone()
                conn.close()
                if row:
                    book_exists = True
                    break
            except Exception as e:
                logger.warning(f"Polling for {ll_bookid} failed: {e}")
            time.sleep(1)
        if not book_exists:
            logger.warning(f"{ll_bookid} never appeared in LL's DB after addBook - marking wanted anyway in case it lands late")

        today = datetime.utcnow().strftime("%Y-%m-%d")
        is_future_release = bool(release_date) and release_date != "0000" and release_date > today
        target_status = "Skipped" if is_future_release else "Wanted"

        # LL defaults every newly-added book to Status=Skipped,
        # AudioStatus=Wanted regardless of which format was actually
        # requested - left alone, that stray Wanted silently triggers a
        # real search+download for a format nobody asked for. The caller
        # (app.py) tells us whether another active local request already
        # covers the other format via `other_active_checker` - kept as a
        # constructor-time callback rather than a param here, see
        # attach_other_active_checker below.
        other_active = self._other_active(ll_bookid, other_type) if self._other_active else False

        def mark_wanted():
            """Goes through LL's own queueBook/unqueueBook/setBookLock API
            commands instead of writing to lazylibrarian.db directly - a
            direct write from here once collided with LL's own concurrent
            writes and corrupted the database
            (lazylibrarian-db-corruption-20260818.md)."""
            api_type = "AudioBook" if status_col == "AudioStatus" else ""
            if target_status == "Wanted":
                self._api("queueBook", id=ll_bookid, type=api_type)

            if not other_active:
                conn = self._db_ro()
                try:
                    row = conn.execute(
                        f"SELECT {other_status_col} as st FROM books WHERE BookID=?", (ll_bookid,)
                    ).fetchone()
                finally:
                    conn.close()
                if row and row["st"] == "Wanted":
                    other_api_type = "AudioBook" if other_status_col == "AudioStatus" else ""
                    self._api("unqueueBook", id=ll_bookid, type=other_api_type)

            # Lock so LL's own author-refresh scan can't silently flip
            # Status/AudioStatus back to its collection-pattern default.
            self._api("setBookLock", id=ll_bookid)

        try:
            mark_wanted()
        except Exception as e:
            logger.error(f"mark_wanted failed for LL book {ll_bookid}: {e}")
            raise

        def recheck_and_fix_drift():
            """addBook's import can still be mid-flight tens of seconds
            after addBook already returned "OK" for an author with a full
            catalog - if that import reads this book's row before our
            lock write reaches the DB, status drifts. Run in the
            background with a generous window rather than blocking the
            request."""
            for _ in range(20):
                time.sleep(3)
                try:
                    conn = self._db_ro()
                    row = conn.execute(
                        f"SELECT {status_col} as st, {other_status_col} as ost FROM books WHERE BookID=?",
                        (ll_bookid,)
                    ).fetchone()
                    conn.close()
                    if row and (row["st"] != target_status or (not other_active and row["ost"] == "Wanted")):
                        logger.info(f"{ll_bookid} status drifted (st={row['st']!r} ost={row['ost']!r}), re-applying")
                        mark_wanted()
                        if not is_future_release:
                            try:
                                self._api("searchBook", id=ll_bookid)
                            except Exception as e:
                                logger.warning(f"searchBook retrigger after drift-fix failed for {ll_bookid}: {e}")
                except Exception as e:
                    logger.warning(f"Post-check for {ll_bookid} failed: {e}")

        threading.Thread(target=recheck_and_fix_drift, daemon=True).start()

        if is_future_release:
            logger.info(f"{ll_bookid} not out until {release_date}, holding at Skipped instead of searching now")
        else:
            try:
                self._api("searchBook", id=ll_bookid)
            except Exception as e:
                logger.warning(f"searchBook trigger failed (will pick up on next scheduled search): {e}")

        return SubmitResult(backend_ref=ll_bookid, resolved=True, status="submitted")

    # app.py sets this after construction - see wiring in app.py. Kept as
    # an attribute rather than a constructor arg so LLBackend can be
    # instantiated before the local-db helper it needs exists.
    _other_active = None

    def attach_other_active_checker(self, fn):
        """fn(backend_ref, other_book_type) -> bool"""
        self._other_active = fn

    # -- status / maintenance --

    def get_status(self, backend_ref, book_type):
        if not backend_ref:
            return None
        try:
            conn = self._db_ro()
            row = conn.execute(
                "SELECT Status, AudioStatus FROM books WHERE BookID=?", (backend_ref,)
            ).fetchone()
            conn.close()
        except Exception as e:
            logger.warning(f"get_status: LL lookup failed for {backend_ref}: {e}")
            return None
        if not row:
            return None
        ll_status = row["AudioStatus"] if book_type == "audiobook" else row["Status"]
        return {
            "Wanted": "wanted",
            "Snatched": "snatched",
            "Open": "downloaded",
            "Have": "downloaded",
        }.get(ll_status)

    def retry_search(self, backend_ref, book_type):
        if backend_ref:
            self._api("searchBook", id=backend_ref)

    def reset_wanted(self, backend_ref, book_type):
        if not backend_ref:
            return
        status_col = "AudioStatus" if book_type == "audiobook" else "Status"
        api_type = "AudioBook" if status_col == "AudioStatus" else ""
        self._api("queueBook", id=backend_ref, type=api_type)

    def poll_maintenance(self, pending_rows):
        """Promote preorders held at Skipped once their release date
        arrives, so LL's normal search cron picks them up starting exactly
        then."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        due = [r for r in pending_rows if r["status"] == "submitted" and r["ll_bookid"]
               and r["release_date"] and r["release_date"] != "0000" and r["release_date"] <= today]
        if not due:
            return
        try:
            conn = self._db_ro()
            try:
                for r in due:
                    status_col = "AudioStatus" if r["book_type"] == "audiobook" else "Status"
                    cur = conn.execute(
                        f"SELECT {status_col} as st FROM books WHERE BookID=?", (r["ll_bookid"],)
                    ).fetchone()
                    if cur and cur["st"] == "Skipped":
                        api_type = "AudioBook" if status_col == "AudioStatus" else ""
                        self._api("queueBook", id=r["ll_bookid"], type=api_type)
                        logger.info(f"{r['ll_bookid']} ({r['title']}) released {r['release_date']}, promoting Skipped -> Wanted")
                        try:
                            self._api("searchBook", id=r["ll_bookid"])
                        except Exception as e:
                            logger.warning(f"searchBook trigger failed for newly-released {r['ll_bookid']}: {e}")
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"preorder promotion check failed: {e}")

    def already_have(self, title, author, book_type):
        try:
            conn = self._db_ro()
        except Exception as e:
            logger.warning(f"Could not open LL db: {e}")
            return False
        status_col = "AudioStatus" if book_type == "audiobook" else "Status"
        try:
            rows = conn.execute(
                f"SELECT b.BookName, b.{status_col} as st FROM books b, authors a "
                "WHERE b.AuthorID = a.AuthorID AND a.AuthorName LIKE ?",
                (f"%{author.split()[-1]}%",)
            ).fetchall()
        finally:
            conn.close()
        target = _normalize(title)
        for r in rows:
            if _normalize(r["BookName"]) == target and r["st"] in ("Open", "Have"):
                return True
        return False


# ---------------------------------------------------------------------------
# Shelfarr
# ---------------------------------------------------------------------------

class ShelfarrBackend(Backend):
    name = "shelfarr"

    def __init__(self, cfg):
        self.cfg = cfg

    def _headers(self):
        return {"Authorization": f"Bearer {self.cfg.SHELFARR_API_KEY}"}

    def _api(self, method, path, **kwargs):
        url = f"{self.cfg.SHELFARR_URL.rstrip('/')}/api/v1/{path.lstrip('/')}"
        r = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()

    def search_ebook(self, query):
        data = self._api("GET", "search", params={"q": query, "limit": 20})
        results = []
        for r in data.get("results", []):
            results.append(NormalizedResult(
                source_id=r.get("work_id"),
                language="",  # not exposed per-result by Shelfarr's search API
                title=r.get("title", ""),
                author=r.get("author", ""),
                cover=r.get("cover_url"),
                release_date=str(r.get("year") or "0000"),
            ))
        return results

    def _find_work_id(self, title, author):
        """No GoodReads-style bridge needed here - Shelfarr's own search
        already returns a work_id directly usable in a request."""
        data = self._api("GET", "search", params={"q": f"{title} {author}", "limit": 5})
        results = data.get("results", [])
        if not results:
            return None
        return results[0].get("work_id")

    def submit(self, *, title, author, book_type, source_id, authorid,
               release_date, needs_resolution):
        work_id = source_id if (source_id and not needs_resolution) else self._find_work_id(title, author)
        if not work_id:
            return SubmitResult(backend_ref=None, resolved=False, status="unresolved")

        try:
            resp = self._api("POST", "requests", json={
                "work_id": work_id,
                "book_types": [book_type],
                "title": title,
                "author": author,
            })
        except requests.HTTPError as e:
            logger.error(f"Shelfarr request create failed for {work_id}: {e}")
            raise

        requests_list = resp.get("requests") or []
        if not requests_list:
            return SubmitResult(backend_ref=None, resolved=False, status="unresolved")
        request_id = requests_list[0]["id"]

        # Shelfarr requests are actionable immediately on creation (its own
        # SearchJob picks them up on the next queue cycle, or right away if
        # immediate_search_enabled is on) - no separate queueBook/
        # setBookLock dance needed, and no dual-format clearing either:
        # Shelfarr tracks ebook/audiobook as separate requests against the
        # same work_id from the start, so there's no shared-row drift to
        # correct in the first place.
        return SubmitResult(backend_ref=str(request_id), resolved=True, status="submitted")

    def get_status(self, backend_ref, book_type):
        if not backend_ref:
            return None
        try:
            data = self._api("GET", f"requests/{backend_ref}")
        except Exception as e:
            logger.warning(f"get_status: Shelfarr lookup failed for {backend_ref}: {e}")
            return None
        shelfarr_status = (data.get("request") or {}).get("status")
        return {
            "pending": "wanted",
            "searching": "wanted",
            "awaiting_purchase": "wanted",
            "downloading": "snatched",
            "processing": "snatched",
            "completed": "downloaded",
            "not_found": None,
            "failed": None,
        }.get(shelfarr_status)

    def retry_search(self, backend_ref, book_type):
        if backend_ref:
            try:
                self._api("POST", f"requests/{backend_ref}/retry")
            except Exception as e:
                logger.warning(f"Shelfarr retry failed for {backend_ref}: {e}")

    def reset_wanted(self, backend_ref, book_type):
        # Shelfarr has no separate "reset to wanted" concept - retry is the
        # equivalent (re-triggers its own SearchJob for the request).
        self.retry_search(backend_ref, book_type)

    def poll_maintenance(self, pending_rows):
        # Shelfarr's own recurring jobs (request_queue, etc.) already
        # handle its side of preorder/queue maintenance internally -
        # nothing for this app to do here.
        return

    def already_have(self, title, author, book_type):
        # Superseded by abs_already_have() in app.py - not used.
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_backend(cfg, ll_db_path):
    return get_backend_by_name(cfg.BACKEND, cfg, ll_db_path)


def get_backend_by_name(name, cfg, ll_db_path):
    """Instantiate a specific backend regardless of the live BACKEND setting.

    Needed because a request's status must always be looked up against
    whichever backend actually created it (recorded per-row in the
    requests.backend column) - not whichever backend happens to be live
    right now. Switching BACKEND only changes where *new* requests go;
    existing in-flight requests from the other backend still need to be
    polled against it until they finish.
    """
    if name == "shelfarr":
        return ShelfarrBackend(cfg)
    return LLBackend(cfg, ll_db_path)
