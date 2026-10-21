#!/usr/bin/env python3
"""Bluesky news crawler — pulls recent posts from a curated list of news
accounts and extracts the article URLs they link to.

Uses the public unauth endpoint at https://public.api.bsky.app/xrpc/...
NOTE: `searchPosts` now requires auth (2025+), so we use `getAuthorFeed`
which is still public.

Source of news handles: news/accounts.txt (one Bluesky handle per line).

Output: appends new URLs (deduped against news.db) to news/urls.txt,
which cron_pipeline.py then feeds into generate.py.

Usage:
  python3 -m crawlers.bluesky                  # use accounts.txt
  python3 -m crawlers.bluesky --days 3         # widen time window
  python3 -m crawlers.bluesky --limit 100      # more posts per account
  python3 -m crawlers.bluesky --print          # don't write, just print
  python3 -m crawlers.bluesky nytimes.com      # ad-hoc single handle
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# allow `python3 crawlers/bluesky.py` and `python3 -m crawlers.bluesky`
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from db import connect

PUBLIC_API = "https://public.api.bsky.app"
NEWS_ROOT = Path(__file__).parent.parent

# Hosts that are never news articles — drop URLs pointing here.
NON_NEWS_DOMAINS = {
    "bsky.app", "bsky.social", "go.bsky.app",
    "x.com", "twitter.com", "t.co",
    "youtube.com", "youtu.be",
    "instagram.com", "facebook.com", "tiktok.com",
    "github.com", "gitlab.com",
    "reddit.com", "old.reddit.com",
    "wikipedia.org",
    "google.com", "linkedin.com",
    "tenor.com", "giphy.com", "imgur.com",
    "open.spotify.com", "apple.co",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url", "source", "fbclid", "gclid",
    "mc_cid", "mc_eid", "share_id", "share", "smid",
}

# Path fragments that mean "this is shopping / service / episode-recap filler",
# not the kind of news worth a《原初种族》-framework commentary. Entertainment
# and fashion news itself is wanted (see the 文娱 section of accounts.txt).
# Match is substring-on-path, case-insensitive.
NOISE_PATH_FRAGMENTS = (
    "curbed.com",           # real estate
    "/strategist/",         # nymag shopping
    "/horoscope", "/horoscopes",
    "/recap/", "-recap-",
    "/podcasts/", "/podcast/",
    "/best-movies", "/best-tv", "/best-shows",
    "/streaming-",
    "/quiz/", "/quizzes/",
    "/crossword", "/games/",
    "/cooking/", "/recipes/",
    "/wirecutter",
)


def http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "breaking-news/0.1 (+howtoraiseiq)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_author_feed(handle: str, limit: int = 50) -> list[dict]:
    """Pull recent posts (no replies, no reposts) from a Bluesky account."""
    params = {
        "actor": handle,
        "limit": str(min(limit, 100)),
        "filter": "posts_no_replies",
    }
    url = f"{PUBLIC_API}/xrpc/app.bsky.feed.getAuthorFeed?{urllib.parse.urlencode(params)}"
    return http_get_json(url).get("feed", [])


def extract_urls(post: dict) -> list[str]:
    """Pull external article links from a post's facets and embed.external."""
    record = post.get("record", {}) or {}
    urls: set[str] = set()

    for facet in (record.get("facets") or []):
        for feat in facet.get("features", []) or []:
            if feat.get("$type") == "app.bsky.richtext.facet#link":
                if feat.get("uri", "").startswith("http"):
                    urls.add(feat["uri"])

    embed = record.get("embed") or {}
    if embed.get("$type") == "app.bsky.embed.external":
        ext = embed.get("external") or {}
        if ext.get("uri", "").startswith("http"):
            urls.add(ext["uri"])

    return list(urls)


def is_news_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    host = host[4:] if host.startswith("www.") else host
    for blocked in NON_NEWS_DOMAINS:
        if host == blocked or host.endswith("." + blocked):
            return False
    # entertainment / lifestyle path filtering
    full = (host + parsed.path).lower()
    for frag in NOISE_PATH_FRAGMENTS:
        if frag in full:
            return False
    return True


def strip_tracking(url: str) -> str:
    p = urllib.parse.urlparse(url)
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    pairs = [(k, v) for k, v in pairs if k not in TRACKING_PARAMS]
    return urllib.parse.urlunparse(p._replace(
        query=urllib.parse.urlencode(pairs),
        fragment="",
    ))


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # bluesky timestamps are like "2026-05-25T18:43:21.123Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def load_accounts(cli_args: list[str]) -> list[str]:
    if cli_args:
        return cli_args
    f = NEWS_ROOT / "accounts.txt"
    if not f.exists():
        sys.exit("no accounts: pass as args or create news/accounts.txt")
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def existing_urls() -> set[str]:
    with connect() as conn:
        return {r["url"] for r in conn.execute("SELECT url FROM articles")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("handles", nargs="*",
                    help="Bluesky handles (default: accounts.txt)")
    ap.add_argument("--days", type=int, default=2,
                    help="only posts from the last N days (default 2)")
    ap.add_argument("--limit", type=int, default=25,
                    help="posts per account (max 100)")
    ap.add_argument("--max-per-account", type=int, default=3,
                    help="max new URLs to keep per account (default 3)")
    ap.add_argument("--max-total", type=int,
                    default=int(os.environ.get("MAX_PER_RUN", "0")),
                    help="cap total new URLs across all accounts (0 = no cap; "
                         "respects MAX_PER_RUN env var)")
    ap.add_argument("--out", default="urls.txt",
                    help="append URLs to this file (relative to news/)")
    ap.add_argument("--print", action="store_true",
                    help="print URLs instead of writing")
    args = ap.parse_args()

    accounts = load_accounts(args.handles)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    in_db = existing_urls()
    found: dict[str, dict] = {}  # url -> {handle}
    stats = {"posts": 0, "new_urls": 0, "filtered_old": 0, "filtered_dom": 0}

    max_total = args.max_total  # 0 = no cap
    for handle in accounts:
        if max_total and len(found) >= max_total:
            print(f"[stop] hit max-total={max_total}", file=sys.stderr)
            break
        try:
            feed = get_author_feed(handle, limit=args.limit)
        except Exception as e:
            print(f"  [error] @{handle}: {e}", file=sys.stderr)
            continue
        kept_for_handle = 0
        for item in feed:
            if kept_for_handle >= args.max_per_account:
                break
            if max_total and len(found) >= max_total:
                break
            post = item.get("post", {})
            record = post.get("record", {}) or {}
            stats["posts"] += 1

            created = parse_iso(record.get("createdAt", ""))
            if created and created < cutoff:
                stats["filtered_old"] += 1
                continue

            for raw_url in extract_urls(post):
                if not is_news_url(raw_url):
                    stats["filtered_dom"] += 1
                    continue
                url = strip_tracking(raw_url)
                if url in in_db or url in found:
                    continue
                found[url] = {"handle": handle}
                kept_for_handle += 1
                stats["new_urls"] += 1
                print(f"  + [@{handle}] {url}", file=sys.stderr)
                if (kept_for_handle >= args.max_per_account
                        or (max_total and len(found) >= max_total)):
                    break
        print(f"[@{handle}] {len(feed)} posts, kept {kept_for_handle}", file=sys.stderr)

    print(f"\n[stats] {stats}", file=sys.stderr)

    if args.print:
        for url in found:
            print(url)
        return

    if not found:
        return

    out = NEWS_ROOT / args.out
    existing_in_file = set()
    if out.exists():
        existing_in_file = {ln.strip() for ln in out.read_text().splitlines()
                            if ln.strip() and not ln.strip().startswith("#")}
    with out.open("a", encoding="utf-8") as f:
        if not existing_in_file:
            f.write(f"# auto-appended by bluesky crawler at {datetime.now().isoformat()}\n")
        for url in found:
            if url not in existing_in_file:
                f.write(url + "\n")
    print(f"[wrote] appended to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
