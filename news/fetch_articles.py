#!/usr/bin/env python3
"""Pull article texts into news.db without writing any commentary.

Collects source material (e.g. entertainment news for the training set):
reads recent posts from the given Bluesky handles, fetches each linked
article, and stores url/source/title/raw_text as an unpublished row with no
commentary. The site never renders these rows; make_dataset.py samples them.

Usage:
  cd news
  python3 fetch_articles.py variety.com vulture.com --per-account 10
  python3 fetch_articles.py --section 文娱 --per-account 10 --days 7
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawlers.bluesky import extract_urls, get_author_feed, is_news_url, parse_iso, strip_tracking
from db import connect
from generate import fetch_article

ACCOUNTS = Path(__file__).parent / "accounts.txt"
MIN_CHARS = 1500  # shorter pages are usually stubs, galleries or video embeds


def section_handles(name: str) -> list[str]:
    """Handles listed under the `# === <name> ...` header in accounts.txt."""
    handles, inside = [], False
    for line in ACCOUNTS.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("# ==="):
            inside = name in s
            continue
        if inside and s and not s.startswith("#"):
            handles.append(s)
    return handles


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="*")
    ap.add_argument("--section", help="take handles from this accounts.txt section, e.g. 文娱")
    ap.add_argument("--per-account", type=int, default=10, help="max new articles per handle")
    ap.add_argument("--days", type=int, default=7, help="only posts from the last N days")
    args = ap.parse_args()

    handles = list(args.handles) + (section_handles(args.section) if args.section else [])
    if not handles:
        sys.exit("give handles or --section")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    with connect() as conn:
        seen = {r["url"] for r in conn.execute("SELECT url FROM articles")}
    total = 0
    for handle in handles:
        try:
            feed = get_author_feed(handle, limit=100)
        except Exception as e:
            print(f"@{handle}: [error] {e}", file=sys.stderr)
            continue
        added = tried = 0
        for item in feed:
            if added >= args.per_account:
                break
            record = item.get("post", {}).get("record", {}) or {}
            created = parse_iso(record.get("createdAt", ""))
            if created and created < cutoff:
                continue
            for raw_url in extract_urls(item.get("post", {})):
                url = strip_tracking(raw_url)
                if not is_news_url(raw_url) or url in seen:
                    continue
                seen.add(url)
                tried += 1
                try:
                    a = fetch_article(url)
                except Exception as e:
                    print(f"  skip {url[:80]}: {e}")
                    continue
                if a["url"] in seen and a["url"] != url:
                    continue
                if len(a["raw_text"]) < MIN_CHARS:
                    print(f"  skip {url[:80]}: only {len(a['raw_text'])} chars")
                    continue
                with connect() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO articles
                           (url, source, source_title, raw_text, fetched_at, published)
                           VALUES (?, ?, ?, ?, ?, 0)""",
                        (a["url"], a["source"], a["source_title"], a["raw_text"],
                         datetime.now(timezone.utc).isoformat()),
                    )
                seen.add(a["url"])
                added += 1
                print(f"  + {a['source']}: {a['source_title'][:70]} ({len(a['raw_text'])} chars)")
                if added >= args.per_account:
                    break
        print(f"@{handle}: {added} added ({tried} tried)")
        total += added
    print(f"\n{total} articles added")


if __name__ == "__main__":
    main()
