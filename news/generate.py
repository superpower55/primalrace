#!/usr/bin/env python3
"""Fetch one news URL, generate Yang Fei-style commentary, store in news.db.

Usage:
  python3 generate.py <url> [<url> ...]
  python3 generate.py --text-file path/to/article.txt --source "name" --url "tag:..."
  python3 generate.py --regen <article_id>     # regenerate commentary for an existing row

LLM provider and keys come from news/.env (PROVIDER, see llm.py).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# load .env from this file's directory regardless of CWD
load_dotenv(Path(__file__).parent / ".env")

import requests                       # noqa: E402
import trafilatura                    # noqa: E402

from db import connect                # noqa: E402
from llm import generate as llm_generate, provider_label  # noqa: E402
from prompts.system import OUTPUT_SCHEMA, build_system_blocks  # noqa: E402


MAX_TOKENS = 2000

# Browser-like UA — most news sites and link shorteners (nyti.ms, reut.rs,
# bloom.bg, n.pr, aje.news, theatln.tc, …) refuse plain-python user agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15"
)

# Sites with hard bot-walls (Cloudflare interactive challenge, login wall, etc.)
# We skip these early — no point spending time on guaranteed failures.
PAYWALL_OR_BLOCKED = (
    "washingtonpost.com",
    "ft.com",
    "wsj.com",
    "nytimes.com/games",
    "bloomberg.com/news/audio",
    "economist.com",   # paywalled past first paragraph
)


# ------------------------ extraction -----------------------------------------

def fetch_article(url: str) -> dict:
    """Download + extract main text.

    Uses requests with a browser UA so shorteners (nyti.ms etc.) follow
    redirects properly and most CDNs don't refuse us.

    Returns {url, source_title, source, raw_text}.
    """
    for blocked in PAYWALL_OR_BLOCKED:
        if blocked in url:
            raise RuntimeError(f"skipped (paywall/blocked): {blocked}")

    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
            allow_redirects=True,
            timeout=25,
        )
    except Exception as e:
        raise RuntimeError(f"network error: {e}")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")

    final_url = resp.url
    html = resp.text
    if not html:
        raise RuntimeError("empty body")

    meta = trafilatura.extract_metadata(html)
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text or len(text) < 200:
        raise RuntimeError(f"could not extract enough text (got {len(text or '')} chars)")

    return {
        "url": final_url,  # resolved URL — dedupe should use this going forward
        "source": (meta.sitename if meta else None) or _host(final_url),
        "source_title": (meta.title if meta else None) or "",
        "raw_text": text.strip(),
    }


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url)
    return m.group(1) if m else url


def _check_env() -> None:
    provider = (os.environ.get("PROVIDER") or "anthropic").lower()
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("PROVIDER=anthropic but ANTHROPIC_API_KEY not set in .env")
    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
        if "ollama.com" in base and not os.environ.get("OLLAMA_API_KEY"):
            sys.exit("OLLAMA_BASE_URL points to Ollama Cloud but OLLAMA_API_KEY not set in .env")


# ------------------------ generation -----------------------------------------

def _strip_json_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def generate_commentary(article: dict) -> dict:
    user_msg = (
        f"以下是今天要评论的一条新闻。\n"
        f"标题：{article['source_title']}\n"
        f"来源：{article['source']}\n"
        f"链接：{article['url']}\n\n"
        f"--- 正文 ---\n{article['raw_text']}\n--- 正文结束 ---\n\n"
        f"按系统提示里的格式输出你的评论。"
    )

    result = llm_generate(build_system_blocks(), user_msg, max_tokens=MAX_TOKENS,
                          json_schema=OUTPUT_SCHEMA)
    text = _strip_json_fence(result["text"])
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model returned non-JSON:\n{text}\n\nerror: {e}")

    return {
        "title": parsed.get("title", "").strip(),
        "axiom": parsed.get("axiom", "").strip(),
        "body": parsed.get("body", "").strip(),
        "title_en": (parsed.get("title_en") or "").strip(),
        "axiom_en": (parsed.get("axiom_en") or "").strip(),
        "body_en": (parsed.get("body_en") or "").strip(),
        "tag": (parsed.get("tag") or "other").strip(),
        "violence_layer": (parsed.get("violence_layer") or "").strip(),
        "model": result["model"],
        "input_tokens": result["input_tokens"],
        "cache_read_tokens": result["cache_read_tokens"],
        "cache_create_tokens": result["cache_create_tokens"],
        "output_tokens": result["output_tokens"],
    }


# ------------------------ orchestration --------------------------------------

def process_url(url: str) -> int:
    """Fetch + generate + insert. Returns the article id."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM articles WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            print(f"  [skip] already in db as #{existing['id']}")
            return existing["id"]

    # Honor admin deletions: if the URL or host has been muted via admin UI,
    # don't waste tokens generating commentary that'll just get deleted again.
    try:
        from admin_server import deletion_filter_reason
        reason = deletion_filter_reason(url)
        if reason:
            print(f"  [muted] {reason}")
            return 0
    except ImportError:
        pass  # admin_server / Flask not installed — skip filter, harmless

    print(f"  [fetch] {url}")
    article = fetch_article(url)
    # If the fetch resolved a shortener (nyti.ms → nytimes.com/...),
    # check dedup against the final URL too — same article via a different
    # short link shouldn't get a second row.
    if article["url"] != url:
        with connect() as conn:
            existing = conn.execute(
                "SELECT id FROM articles WHERE url = ?", (article["url"],)
            ).fetchone()
            if existing:
                print(f"  [skip] resolved to existing #{existing['id']}: {article['url']}")
                return existing["id"]
    print(f"  [extract] {len(article['raw_text'])} chars — {article['source_title'][:60]}")

    print(f"  [generate] {provider_label()}")
    commentary = generate_commentary(article)
    print(f"  [done] '{commentary['title']}' "
          f"(in={commentary['input_tokens']} "
          f"cache_r={commentary['cache_read_tokens']} "
          f"cache_w={commentary['cache_create_tokens']} "
          f"out={commentary['output_tokens']})")

    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO articles
              (url, source, source_title, raw_text, fetched_at,
               title, axiom, body, title_en, axiom_en, body_en,
               tag, violence_layer,
               model, input_tokens, cache_read_tokens, cache_create_tokens,
               output_tokens, generated_at)
            VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?, ?,?,?,?,?,?)
            """,
            (
                article["url"], article["source"], article["source_title"],
                article["raw_text"], now,
                commentary["title"], commentary["axiom"], commentary["body"],
                commentary["title_en"], commentary["axiom_en"], commentary["body_en"],
                commentary["tag"], commentary["violence_layer"],
                commentary["model"], commentary["input_tokens"],
                commentary["cache_read_tokens"], commentary["cache_create_tokens"],
                commentary["output_tokens"], now,
            ),
        )
        return cur.lastrowid


def regen(article_id: int) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT url, source, source_title, raw_text FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    if not row:
        raise SystemExit(f"no article with id {article_id}")
    article = dict(row)
    print(f"  [regen #{article_id}] {article['source_title'][:60]}  ({provider_label()})")
    commentary = generate_commentary(article)
    print(f"  [done] '{commentary['title']}'")
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """UPDATE articles SET
                 title=?, axiom=?, body=?,
                 title_en=?, axiom_en=?, body_en=?,
                 tag=?, violence_layer=?,
                 model=?, input_tokens=?, cache_read_tokens=?,
                 cache_create_tokens=?, output_tokens=?, generated_at=?
               WHERE id=?""",
            (
                commentary["title"], commentary["axiom"], commentary["body"],
                commentary["title_en"], commentary["axiom_en"], commentary["body_en"],
                commentary["tag"], commentary["violence_layer"],
                commentary["model"], commentary["input_tokens"],
                commentary["cache_read_tokens"], commentary["cache_create_tokens"],
                commentary["output_tokens"], now, article_id,
            ),
        )


# ------------------------ CLI ------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", help="news article URLs to process")
    ap.add_argument("--text-file", help="local text file instead of URL")
    ap.add_argument("--source", default="manual", help="for --text-file")
    ap.add_argument("--url", default=None, help="for --text-file: synthetic url/id")
    ap.add_argument("--title", default="", help="for --text-file")
    ap.add_argument("--regen", type=int, help="regenerate commentary for existing id")
    args = ap.parse_args()

    _check_env()

    if args.regen:
        regen(args.regen)
        return

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
        url = args.url or f"local:{Path(args.text_file).name}"
        article = {
            "url": url, "source": args.source,
            "source_title": args.title or Path(args.text_file).stem,
            "raw_text": text,
        }
        with connect() as conn:
            if conn.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
                sys.exit(f"already exists: {url}")
        commentary = generate_commentary(article)
        print(f"  [done] '{commentary['title']}'")
        now = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            conn.execute(
                """INSERT INTO articles
                     (url, source, source_title, raw_text, fetched_at,
                      title, axiom, body, title_en, axiom_en, body_en,
                      tag, violence_layer,
                      model, input_tokens, cache_read_tokens, cache_create_tokens,
                      output_tokens, generated_at)
                   VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?, ?,?,?,?,?,?)""",
                (url, args.source, article["source_title"], text, now,
                 commentary["title"], commentary["axiom"], commentary["body"],
                 commentary["title_en"], commentary["axiom_en"], commentary["body_en"],
                 commentary["tag"], commentary["violence_layer"],
                 commentary["model"], commentary["input_tokens"],
                 commentary["cache_read_tokens"], commentary["cache_create_tokens"],
                 commentary["output_tokens"], now),
            )
        return

    if not args.urls:
        ap.print_help()
        sys.exit(1)

    for url in args.urls:
        try:
            process_url(url)
        except Exception as e:
            print(f"  [error] {url}: {e}")


if __name__ == "__main__":
    main()
