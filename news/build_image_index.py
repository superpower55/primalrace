#!/usr/bin/env python3
"""Build the image index for search by image (served by api/image-search.js).

Three steps per run, each capped so a free-tier Gemini key is never used up
and a run resumes where the last one stopped:

  1. find each published article's lead image (og:image) -> articles.image_url
     ('' when the page has none or cannot be fetched, so it is not retried)
  2. have Gemini describe the image -> articles.image_desc
  3. embed the descriptions -> news/search/images/{vectors.bin,meta.json,info.json}

A searcher's photo is described with the same prompt (api/image-search.js),
so "similar image" means "similar description".

  GEMINI_API_KEY        from https://aistudio.google.com/apikey
  GEMINI_VISION_MODEL   default gemini-2.5-flash-lite
  GEMINI_EMBED_MODEL    default gemini-embedding-001

Usage:
  cd news
  GEMINI_API_KEY=... python3 build_image_index.py --max-fetch 1500 --max-captions 600
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import build_search_index as text_index  # noqa: E402  (embed_batch, quantize, DIMS)
from db import connect  # noqa: E402
from generate import USER_AGENT  # noqa: E402

OUT_DIR = Path(__file__).parent / "search" / "images"
VECTORS = OUT_DIR / "vectors.bin"
META = OUT_DIR / "meta.json"
INFO = OUT_DIR / "info.json"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/"
MAX_IMAGE_BYTES = 6_000_000

# Keep in sync with DESCRIBE_PROMPT in api/image-search.js: the searcher's
# photo must be described the same way for the comparison to mean anything.
DESCRIBE_PROMPT = (
    "这是一张新闻配图。用中文写一段 60 到 120 字的客观描述：画面里有哪些人、物、场景、动作，"
    "看得到的文字和标志，以及整体氛围。只写看得到的东西，不要猜测人物身份（除非图里的文字写明），"
    "不要评论。最后另起一行，用英文列出 5 到 10 个关键词，用逗号分隔。"
)

OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I)


def http_get(url: str, timeout: int = 25, accept: str = "*/*") -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_IMAGE_BYTES + 1), r.headers.get("Content-Type", "")


def find_image(url: str) -> str:
    # Same request as generate.fetch_article: several sites (CNBC, Billboard,
    # Deadline) refuse a bare urllib request but serve this one.
    try:
        resp = requests.get(url, timeout=25, allow_redirects=True, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        if resp.status_code >= 400:
            return ""
        html = resp.text[:2_000_000]  # some sites put og:image ~600k chars in
    except Exception:
        return ""
    m = OG_IMAGE.search(html)
    return (m.group(1) or m.group(2)).replace("&amp;", "&").strip() if m else ""


def describe(image_url: str, model: str, key: str) -> str:
    data, ctype = http_get(image_url, accept="image/*")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")
    mime = ctype.split(";")[0].strip() if ctype.startswith("image/") else "image/jpeg"
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
        {"text": DESCRIBE_PROMPT},
    ]}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400}}
    req = urllib.request.Request(f"{GEMINI}{model}:generateContent?key={key}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    parts = (((out.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise ValueError("empty description")
    return text


def step_find_images(max_fetch: int, deadline: float) -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, url FROM articles WHERE published = 1 AND image_url IS NULL
               ORDER BY generated_at DESC LIMIT ?""", (max_fetch,)).fetchall()
    print(f"[1/3] finding lead images for {len(rows)} articles")
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, len(rows), 40):
            if time.time() > deadline:
                print("  time limit reached")
                break
            chunk = rows[i:i + 40]
            found = list(pool.map(lambda r: find_image(r["url"]), chunk))
            with connect() as conn:
                conn.executemany("UPDATE articles SET image_url = ? WHERE id = ?",
                                 [(img, r["id"]) for img, r in zip(found, chunk)])
            done += len(chunk)
            print(f"  {done}/{len(rows)} checked, {sum(1 for f in found if f)} of the last {len(chunk)} have an image")


def step_describe(max_captions: int, model: str, key: str, deadline: float) -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, image_url FROM articles
               WHERE published = 1 AND image_url != '' AND image_desc IS NULL
               ORDER BY generated_at DESC LIMIT ?""", (max_captions,)).fetchall()
    print(f"[2/3] describing {len(rows)} images with {model}")
    done = failures = 0
    for r in rows:
        if time.time() > deadline:
            print("  time limit reached")
            break
        try:
            desc = describe(r["image_url"], model, key)
            failures = 0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                failures += 1
                if failures > 3:
                    print("  Gemini quota used up; the next run continues")
                    break
                time.sleep(30)
                continue
            if e.code >= 500:
                continue
            desc = ""  # 4xx on the image or the request: don't retry this one
        except Exception:
            desc = ""  # image unreachable / unreadable
        with connect() as conn:
            conn.execute("UPDATE articles SET image_desc = ? WHERE id = ?", (desc, r["id"]))
        done += 1
        if done % 25 == 0:
            print(f"  {done} described")
        time.sleep(4)  # stay under free-tier requests-per-minute
    print(f"  {done} described this run")


def load_previous() -> dict[tuple[int, str], bytes]:
    if not (META.exists() and VECTORS.exists()):
        return {}
    meta = json.loads(META.read_text(encoding="utf-8"))
    raw = VECTORS.read_bytes()
    dims = text_index.DIMS
    if len(raw) != len(meta) * dims:
        return {}
    return {(m["id"], m["h"]): raw[i * dims:(i + 1) * dims] for i, m in enumerate(meta)}


def step_embed(model: str, key: str) -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, image_url, image_desc, title, title_en, generated_at FROM articles
               WHERE published = 1 AND image_desc IS NOT NULL AND image_desc != ''
               ORDER BY generated_at DESC""").fetchall()
    prev = load_previous()
    entries = []
    for r in rows:
        h = text_index.text_hash(r["image_desc"])
        entries.append([{"id": r["id"], "h": h, "img": r["image_url"],
                         "c": r["image_desc"].split("\n")[0][:120],
                         "t": r["title"] or "", "te": r["title_en"] or "",
                         "d": (r["generated_at"] or "")[:10]},
                        r["image_desc"], prev.get((r["id"], h))])
    todo = [e for e in entries if e[2] is None]
    print(f"[3/3] {len(entries)} described images, {len(todo)} to embed")
    for i in range(0, len(todo), text_index.BATCH):
        batch = todo[i:i + text_index.BATCH]
        try:
            vecs = text_index.embed_batch([e[1] for e in batch], [e[0]["t"] for e in batch], model, key)
        except urllib.error.HTTPError as e:
            print(f"  embedding stopped (HTTP {e.code}); the next run continues")
            break
        for e, v in zip(batch, vecs):
            e[2] = text_index.quantize(v)
    ready = [e for e in entries if e[2] is not None]
    if not ready:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        VECTORS: b"".join(e[2] for e in ready),
        META: json.dumps([e[0] for e in ready], ensure_ascii=False, separators=(",", ":")).encode(),
        INFO: json.dumps({"model": model, "dims": text_index.DIMS, "count": len(ready),
                          "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}).encode(),
    }
    for path, data in files.items():
        path.with_suffix(path.suffix + ".tmp").write_bytes(data)
    for path in files:
        path.with_suffix(path.suffix + ".tmp").replace(path)
    print(f"  image index: {len(ready)} images")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-fetch", type=int, default=1500, help="articles to look up images for")
    ap.add_argument("--max-captions", type=int, default=600, help="images to describe")
    ap.add_argument("--max-minutes", type=float, default=150)
    args = ap.parse_args()

    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        print("GEMINI_API_KEY not set — image index left unchanged")
        return
    vision = os.environ.get("GEMINI_VISION_MODEL") or "gemini-2.5-flash-lite"
    embed = os.environ.get("GEMINI_EMBED_MODEL") or "gemini-embedding-001"
    start = time.time()
    deadline = start + args.max_minutes * 60

    # finding images gets at most 30% of the time; describing them the rest
    step_find_images(args.max_fetch, start + args.max_minutes * 60 * 0.3)
    step_describe(args.max_captions, vision, key, deadline)
    step_embed(embed, key)


if __name__ == "__main__":
    sys.exit(main())
