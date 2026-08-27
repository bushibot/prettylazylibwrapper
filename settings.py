import os
import sqlite3

LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "/data/prettylazylibwrapper.db")

# (key, label, is_secret, default) - drives both the live config lookups below
# and the /config page's rendered form. Env vars of the same name seed a fresh
# install; values saved via /config take precedence after that.
SETTING_FIELDS = [
    ("BACKEND", "Acquisition backend: lazylibrarian or shelfarr", False, "lazylibrarian"),
    ("LL_URL", "LazyLibrarian URL", False, ""),
    ("LL_API_KEY", "LazyLibrarian API key", True, ""),
    ("SHELFARR_URL", "Shelfarr URL (used when BACKEND=shelfarr)", False, ""),
    ("SHELFARR_API_KEY", "Shelfarr API token (used when BACKEND=shelfarr)", True, ""),
    ("ABS_URL", "Audiobookshelf URL, used for the already-owned check (independent of BACKEND)", False, ""),
    ("ABS_API_KEY", "Audiobookshelf API token", True, ""),
    ("ABS_AUDIOBOOK_LIBRARY_ID", "Audiobookshelf audiobook library ID", False, ""),
    ("ABS_EBOOK_LIBRARY_ID", "Audiobookshelf ebook library ID", False, ""),
    ("QBIT_URL", "qBittorrent URL", False, ""),
    ("QBIT_USER", "qBittorrent username", False, "admin"),
    ("QBIT_PASS", "qBittorrent password", True, ""),
    ("SAB_URL", "SABnzbd URL (optional)", False, ""),
    ("SAB_API_KEY", "SABnzbd API key (optional)", True, ""),
    ("ABB_BASE", "AudioBookBay base URL, used by the built-in indexer bridge", False, "https://audiobookbay.lu"),
    ("BRIDGE_API_KEY", "AudioBookBay bridge API key (also set this in Prowlarr's indexer)", True, ""),
    ("ABB_PROXY_URL", "HTTP proxy for reaching AudioBookBay (e.g. http://host:port) - some ISPs block it directly, leave blank to connect direct", False, ""),
    ("NYT_API_KEY", "NYT Books API key, used for the ebook New Releases browse (developer.nytimes.com)", True, ""),
    ("ABB_MIN_REQUEST_INTERVAL", "Minimum seconds between requests to AudioBookBay - a single LL search can trigger several (one per result, for detail pages), and a 15-min cron pass searches every wanted book, so bursts add up fast. Raise this if you keep getting rate-limited/blocked.", False, "3"),
]


def _db():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_settings_db():
    conn = _db()
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


def get_setting(key, default=""):
    conn = _db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    if row and row["value"] not in (None, ""):
        return row["value"]
    return os.environ.get(key, default)


def set_setting(key, value):
    conn = _db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_users():
    raw = get_setting("USERS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def set_users(users_list):
    set_setting("USERS", ",".join(u.strip() for u in users_list if u.strip()))


class LiveConfig:
    """Attribute access resolves fresh from the settings db (falling back to
    env vars) on every read, so changes made on /config take effect
    immediately without a container restart."""

    _DEFAULTS = {key: default for key, _label, _secret, default in SETTING_FIELDS}

    def __getattr__(self, name):
        if name not in self._DEFAULTS:
            raise AttributeError(name)
        return get_setting(name, self._DEFAULTS[name])


cfg = LiveConfig()


def get_all_settings_masked():
    """For rendering the config form - secrets come back as a fixed placeholder
    if set, never their real value, so they're not exposed over the LAN just by
    loading the page. The frontend only sends a field's value back on save if
    the admin actually changed it."""
    out = {}
    for key, label, is_secret, _default in SETTING_FIELDS:
        value = get_setting(key, "")
        out[key] = {
            "label": label,
            "secret": is_secret,
            "value": ("••••••••" if (is_secret and value) else value),
            "set": bool(value),
        }
    out["USERS"] = {"label": "Household users", "secret": False, "value": ", ".join(get_users()), "set": True}
    return out
