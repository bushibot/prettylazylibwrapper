import os
import re
import json
import time
import random
import logging
import threading
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import Response

from settings import cfg

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("abb-bridge")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CACHE_TTL = 600  # seconds, avoid re-hitting a detail page repeatedly across requests

app = FastAPI()

_detail_cache: dict[str, tuple[float, dict]] = {}

# LL's own cron re-searches every wanted book every 15 minutes, but a given
# audiobook only ever shows up on ABB once - checking dozens of times a day
# is pure wasted load against a scraped site with no real API. One real check
# per book per day is plenty (new releases don't appear more than once a
# week per the household's own usage), and this doubles as the on-demand
# path too: whichever request happens to be the first one for a given book
# that day (cron or a human searching) is the one that actually reaches ABB;
# everything else that day for the same book is served from this cache.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEARCH_CACHE_PATH = os.path.join(DATA_DIR, "abb_search_cache.json")
SEARCH_CACHE_TTL = 24 * 60 * 60
_search_cache_lock = threading.Lock()


def _load_search_cache() -> dict:
    try:
        with open(SEARCH_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_search_cache(cache: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SEARCH_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, SEARCH_CACHE_PATH)


def _book_key(q: str, author: str, title: str) -> str:
    raw = q or f"{author} {title}"
    return re.sub(r"\s+", " ", raw.strip().lower())

HEALTH_CACHE_TTL = 120  # seconds - a real connectivity check on every poll would
                        # itself look like scraping traffic against the site
_health_cache: dict = {"status": "unknown", "checked_at": None, "detail": ""}


def _proxies():
    """Some ISPs (confirmed: Comcast) silently block audiobookbay.lu outright -
    routing through an HTTP proxy (e.g. a VPN sidecar) works around that. Empty
    setting means connect direct, same as before this existed."""
    url = cfg.ABB_PROXY_URL
    return {"http": url, "https": url} if url else None


def check_abb_health() -> dict:
    """Cached, so the frontend can poll this often without adding real load
    against the site itself."""
    now = time.time()
    if _health_cache["checked_at"] and now - _health_cache["checked_at"] < HEALTH_CACHE_TTL:
        return _health_cache
    try:
        _throttle()
        r = requests.get(cfg.ABB_BASE + "/", headers=HEADERS, timeout=10, proxies=_proxies())
        r.raise_for_status()
        _health_cache.update(status="ok", detail="")
    except Exception as e:
        _health_cache.update(status="down", detail=str(e))
    _health_cache["checked_at"] = now
    return _health_cache


_rate_lock = threading.Lock()
_last_request_at = 0.0


def _throttle():
    """Every outbound request to ABB funnels through fetch() (search pages
    and detail pages alike), so this is the one place that needs to enforce
    spacing. A single LL search can fan out into several detail-page fetches,
    and LL's cron searches every wanted book in one pass - without this,
    a household with a handful of wanted books can burst dozens of requests
    within seconds, which reads as bot/abuse traffic to ABB's own protection.

    A perfectly exact interval every time is itself a bot signature real
    traffic never has, so jitter is added on top - never subtracted, since
    the whole point is guaranteeing a floor, not averaging one."""
    global _last_request_at
    with _rate_lock:
        min_interval = float(cfg.ABB_MIN_REQUEST_INTERVAL or 3)
        jitter = random.uniform(0, min_interval * 0.5)
        wait = _last_request_at + min_interval + jitter - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def fetch(url: str) -> tuple[str, str]:
    """Returns (html, final_url) - final_url lets callers detect when ABB
    silently redirected a search away from the results page."""
    _throttle()
    r = requests.get(url, headers=HEADERS, timeout=20, proxies=_proxies())
    r.raise_for_status()
    return r.text, r.url


def search_abb(query: str, limit: int = 25) -> list[dict]:
    """Search ABB and return a list of {title, url} for candidate books.
    Empty query falls back to the homepage's recent-releases listing, so
    indexer connection tests (which probe with no query) get real results."""
    is_search = bool(query.strip())
    if is_search:
        # ABB's search silently redirects to the homepage for queries
        # containing uppercase letters - lowercase everything sent to it.
        url = f"{cfg.ABB_BASE}/?s={urllib.parse.quote_plus(query.lower())}"
    else:
        url = cfg.ABB_BASE + "/"
    html, final_url = fetch(url)
    if is_search and "?s=" not in final_url:
        # ABB silently bounced the search back to the homepage instead of
        # showing (possibly empty) results - treat as zero matches rather
        # than parsing the homepage's unrelated recent-releases listing.
        log.warning("search for %r was redirected to %s, treating as no results", query, final_url)
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for a in soup.select('a[href*="/abss/"]'):
        href = a.get("href", "")
        if not href or href in seen:
            continue
        # skip nav/pagination links that aren't real book pages
        if href.rstrip("/").split("/")[-1] == "":
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        seen.add(href)
        results.append({"title": title, "url": href if href.startswith("http") else cfg.ABB_BASE + href})
        if len(results) >= limit:
            break
    return results


def parse_detail(url: str) -> dict | None:
    """Fetch a book detail page and extract hash/trackers/size. Cached briefly."""
    now = time.time()
    cached = _detail_cache.get(url)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        html, _ = fetch(url)
    except Exception as e:
        log.warning("failed to fetch detail page %s: %s", url, e)
        return None

    soup = BeautifulSoup(html, "lxml")

    info_hash = None
    for td in soup.find_all("td"):
        text = td.get_text(strip=True)
        if text in ("Info Hash:", "Info:"):
            nxt = td.find_next_sibling("td")
            if nxt:
                candidate = nxt.get_text(strip=True)
                if re.fullmatch(r"[a-fA-F0-9]{40}", candidate):
                    info_hash = candidate
                    break

    if not info_hash:
        return None

    trackers = sorted(set(re.findall(r'(?:udp|https?)://[a-zA-Z0-9.\-]+:\d+/announce', html)))

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url.rstrip("/").split("/")[-1]

    size_bytes = 0
    for td in soup.find_all("td"):
        if td.get_text(strip=True) == "File Size:":
            nxt = td.find_next_sibling("td")
            if nxt:
                m = re.search(r'([\d.]+)\s*(MB|GB|KB)', nxt.get_text(strip=True), re.I)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2).upper()
                    mult = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit]
                    size_bytes = int(val * mult)
            break

    result = {
        "title": title,
        "info_hash": info_hash,
        "trackers": trackers,
        "size_bytes": size_bytes or 0,
        "url": url,
    }
    _detail_cache[url] = (now, result)
    return result


STOPWORDS = {"a", "an", "the", "of", "and", "book", "novel"}


def significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def filter_relevant(candidates: list[dict], query: str, min_overlap: float = 0.5) -> list[dict]:
    """Keep only search results whose title actually overlaps with the query.
    ABB's own search returns loosely-related noise; without this, junk
    results get passed upstream as if they were real matches."""
    q_words = significant_words(query)
    if not q_words:
        return candidates
    kept = []
    for c in candidates:
        t_words = significant_words(c["title"])
        overlap = len(q_words & t_words) / len(q_words)
        if overlap >= min_overlap:
            kept.append(c)
    return kept


def build_magnet(item: dict) -> str:
    # xt must stay as literal "urn:btih:<hash>" - strict magnet parsers
    # (e.g. Prowlarr/MonoTorrent) reject it if the colons get percent-encoded,
    # so this part is built by hand instead of going through urlencode().
    parts = [f"xt=urn:btih:{item['info_hash']}", f"dn={urllib.parse.quote(item['title'])}"]
    for t in item["trackers"]:
        parts.append(f"tr={urllib.parse.quote(t, safe=':/')}")
    return "magnet:?" + "&".join(parts)


CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="AudioBookBay Bridge" strapline="ABB Torznab bridge"/>
  <limits max="50" default="25"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <book-search available="yes" supportedParams="q,author,title"/>
  </searching>
  <categories>
    <category id="3030" name="Audio/Audiobook"/>
  </categories>
</caps>"""


def rss_item(item: dict) -> str:
    magnet = build_magnet(item)
    title = (
        item["title"]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    magnet_xml = magnet.replace("&", "&amp;")
    return f"""    <item>
      <title>{title}</title>
      <guid isPermaLink="false">abb-{item['info_hash']}</guid>
      <link>{magnet_xml}</link>
      <comments>{item['url']}</comments>
      <pubDate>{pub_date}</pubDate>
      <size>{item['size_bytes']}</size>
      <category>3030</category>
      <enclosure url="{magnet_xml}" length="{item['size_bytes']}" type="application/x-bittorrent"/>
      <torznab:attr name="category" value="3030"/>
      <torznab:attr name="size" value="{item['size_bytes']}"/>
      <torznab:attr name="infohash" value="{item['info_hash']}"/>
      <torznab:attr name="magneturl" value="{magnet_xml}"/>
      <torznab:attr name="seeders" value="1"/>
      <torznab:attr name="peers" value="1"/>
      <torznab:attr name="downloadvolumefactor" value="0"/>
      <torznab:attr name="uploadvolumefactor" value="1"/>
    </item>"""


def rss_wrapper(items: list[dict]) -> str:
    body = "\n".join(rss_item(i) for i in items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>AudioBookBay Bridge</title>
    <description>ABB Torznab bridge</description>
{body}
  </channel>
</rss>"""


@app.get("/api")
def api(
    apikey: str = Query(default=""),
    t: str = Query(default="search"),
    q: str = Query(default=""),
    author: str = Query(default=""),
    title: str = Query(default=""),
    limit: int = Query(default=25),
):
    if t == "caps":
        return Response(content=CAPS_XML, media_type="application/xml")

    if apikey != cfg.BRIDGE_API_KEY:
        return Response(
            content='<?xml version="1.0"?><error code="100" description="Invalid API key"/>',
            media_type="application/xml",
            status_code=401,
        )

    book_key = _book_key(q, author, title)
    with _search_cache_lock:
        cached = _load_search_cache().get(book_key)
    if cached and time.time() - cached["checked_at"] < SEARCH_CACHE_TTL:
        age_hrs = (time.time() - cached["checked_at"]) / 3600
        log.info("book_key=%r served from cache (checked %.1fh ago), skipping ABB", book_key, age_hrs)
        return Response(content=rss_wrapper(cached["items"]), media_type="application/xml")

    # Build query attempts in priority order. ABB's own search is a fussy
    # WordPress fuzzy search - "title only" often finds nothing while
    # "author + title" or "author only" succeeds, so try progressively
    # narrower fallbacks rather than a single fixed query.
    attempts = []
    if q:
        attempts.append(q)
    if author and title:
        attempts.append(f"{author} {title}")
    if title:
        attempts.append(title)
    if author:
        attempts.append(author)
    if not attempts:
        attempts.append("")  # recent-releases fallback

    search_terms = q or f"{author} {title}".strip()
    log.info("search attempts=%r", attempts)

    candidates = []
    used_query = None
    for attempt in attempts:
        try:
            found = search_abb(attempt, limit=min(limit, 50))
        except Exception as e:
            log.error("search failed for %r: %s", attempt, e)
            continue
        relevant = filter_relevant(found, search_terms) if attempt else found
        if relevant:
            candidates = relevant
            used_query = attempt
            break
    log.info("used_query=%r -> %d relevant candidates", used_query, len(candidates))

    items = []
    for c in candidates:
        detail = parse_detail(c["url"])
        if detail:
            items.append(detail)

    log.info("used_query=%r -> %d candidates, %d with usable magnet data", used_query, len(candidates), len(items))

    with _search_cache_lock:
        cache = _load_search_cache()
        cache[book_key] = {"checked_at": time.time(), "items": items}
        _save_search_cache(cache)

    return Response(content=rss_wrapper(items), media_type="application/xml")


@app.get("/health")
def health():
    """Reports this bridge's own liveness, not the upstream site - use
    /source-status for whether audiobookbay.lu itself is reachable."""
    return {"status": "ok"}


@app.get("/source-status")
def source_status():
    return check_abb_health()
