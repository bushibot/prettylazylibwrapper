#!/usr/bin/env python3
"""Queue reviewed 'expired' requests for a throttled revival.

Usage (inside the container):
    python3 revive_queue.py /data/reviewed.csv            # dry run: show what would be queued
    python3 revive_queue.py /data/reviewed.csv --apply    # actually queue them
    python3 revive_queue.py --status                      # queue size + next few up
    python3 revive_queue.py --clear --apply               # empty the queue

The CSV is the export of expired requests with a 're_want (y/n)' column and a
'pllw_id' column. Only rows marked y/yes are queued. Queuing does NOT change
anything by itself - app.py's revive_trickle() then releases queued rows back
to 'wanted' at REVIVE_PER_DAY, evenly spaced, in this priority order:

    2  requested by a named person (not 'Everyone', not a watchlist auto-request)
    1  wrongly expired by the old release-date bug (released < 30d before expiry)
    0  everything else
    ...then most recently requested first within each tier.
"""
import csv
import sqlite3
import sys
from datetime import datetime

DB = "/data/prettylazylibwrapper.db"


def priority(row):
    who = (row["requested_by"] or row["requested_for"] or "").strip()
    if who and who != "Everyone" and not who.startswith("Watchlist:"):
        return 2
    rel, upd = row["release_date"], row["updated_at"]
    if rel and upd:
        try:
            if (datetime.strptime(upd[:10], "%Y-%m-%d") - datetime.strptime(rel[:10], "%Y-%m-%d")).days < 30:
                return 1
        except ValueError:
            pass
    return 0


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if "--status" in args:
        n = conn.execute("SELECT COUNT(*) FROM requests WHERE status='expired' AND revive_queued_at IS NOT NULL").fetchone()[0]
        print(f"queued: {n}")
        for r in conn.execute(
            "SELECT title, author, revive_priority, created_at FROM requests "
            "WHERE status='expired' AND revive_queued_at IS NOT NULL "
            "ORDER BY COALESCE(revive_priority,0) DESC, created_at DESC LIMIT 10"):
            print(f"  p{r['revive_priority']} {r['created_at'][:10]}  {r['title']} - {r['author']}")
        day = conn.execute("SELECT COUNT(*) FROM requests WHERE wanted_since > datetime('now','-1 day')").fetchone()[0]
        print(f"revived in last 24h: {day}")
        return

    if "--clear" in args:
        n = conn.execute("SELECT COUNT(*) FROM requests WHERE revive_queued_at IS NOT NULL").fetchone()[0]
        print(f"{'clearing' if apply else 'would clear'} {n} queued rows")
        if apply:
            conn.execute("UPDATE requests SET revive_queued_at=NULL, revive_priority=NULL WHERE revive_queued_at IS NOT NULL")
            conn.commit()
        return

    path = next((a for a in args if not a.startswith("--")), None)
    if not path:
        sys.exit(__doc__)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    want_col = next((k for k in rows[0] if k and k.lower().startswith("re_want")), None)
    if not want_col or "pllw_id" not in rows[0]:
        sys.exit("CSV needs a 're_want' column and a 'pllw_id' column")
    ids = [int(r["pllw_id"]) for r in rows if (r[want_col] or "").strip().lower() in ("y", "yes")]
    print(f"{len(rows)} rows in CSV, {len(ids)} marked yes")

    now = datetime.utcnow().isoformat()
    tiers, skipped = {0: 0, 1: 0, 2: 0}, 0
    for i in ids:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (i,)).fetchone()
        if not row or row["status"] != "expired":
            skipped += 1
            continue
        p = priority(row)
        tiers[p] += 1
        if apply:
            conn.execute("UPDATE requests SET revive_queued_at=?, revive_priority=? WHERE id=?", (now, p, i))
    if apply:
        conn.commit()
    print(f"{'queued' if apply else 'would queue'}: priority2={tiers[2]} priority1={tiers[1]} priority0={tiers[0]}"
          f" (skipped {skipped} no longer expired/missing)")
    if not apply:
        print("dry run - re-run with --apply")


if __name__ == "__main__":
    main()
