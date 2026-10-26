#!/usr/bin/env python3
"""Build the semantic search index for /news (served by api/search.js).

Embeds every published commentary with Gemini and writes:
  news/search/vectors.bin  int8 rows, L2-normalised then scaled by 127
  news/search/meta.json    one entry per row: id, titles, axioms, date, tag, snippet, hash
  news/search/info.json    model, dims, count, build time

Incremental: rows whose text hash is unchanged keep their vector from the
previous index, so a normal run only embeds the new articles. A run that
hits --max-minutes or a quota error writes what it has; the next run
continues. Without GEMINI_API_KEY it exits 0 and leaves the index alone.

  GEMINI_API_KEY        from https://aistudio.google.com/apikey
  GEMINI_EMBED_MODEL    default gemini-embedding-001

Usage:
  cd news
  GEMINI_API_KEY=... python3 build_search_index.py
  python3 build_search_index.py --max-minutes 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from db import connect  # noqa: E402

OUT_DIR = Path(__file__).parent / "search"
VECTORS = OUT_DIR / "vectors.bin"
META = OUT_DIR / "meta.json"
INFO = OUT_DIR / "info.json"

DIMS = 768
BATCH = 50
BODY_CHARS = 1500     # enough to capture the argument; keeps token use low
SNIPPET_CHARS = 90
SAVE_EVERY = 120      # seconds between checkpoints


def doc_text(r) -> str:
    body = (r["your_edits"] or r["body"] or "").strip()
    parts = [r["title"], r["axiom"], body[:BODY_CHARS], r["title_en"], r["axiom_en"]]
    return "\n".join(p.strip() for p in parts if p and p.strip())


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def quantize(vec: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return bytes((max(-127, min(127, round(x / norm * 127))) & 0xFF) for x in vec)


def embed_batch(texts: list[str], titles: list[str], model: str, key: str) -> list[list[float]]:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":batchEmbedContents?key={key}")
    body = {"requests": [{
        "model": f"models/{model}",
        "content": {"parts": [{"text": t}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "title": title,
        "outputDimensionality": DIMS,
    } for t, title in zip(texts, titles)]}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    vecs = [e["values"] for e in data["embeddings"]]
    if len(vecs) != len(texts) or any(len(v) != DIMS for v in vecs):
        raise RuntimeError("unexpected embedding response shape")
    return vecs


def load_previous() -> dict[tuple[int, str], bytes]:
    """(id, hash) -> int8 row from the existing index, if it matches DIMS."""
    if not (META.exists() and VECTORS.exists() and INFO.exists()):
        return {}
    info = json.loads(INFO.read_text(encoding="utf-8"))
    if info.get("dims") != DIMS:
        return {}
    meta = json.loads(META.read_text(encoding="utf-8"))
    raw = VECTORS.read_bytes()
    if len(raw) != len(meta) * DIMS:
        return {}
    return {(m["id"], m["h"]): raw[i * DIMS:(i + 1) * DIMS] for i, m in enumerate(meta)}


def write_index(entries: list, model: str) -> int:
    """Write every entry that has a vector. Called periodically and at the end,
    so a cancelled or timed-out run keeps what it embedded."""
    ready = [e for e in entries if e[2] is not None]
    OUT_DIR.mkdir(exist_ok=True)
    files = {
        VECTORS: b"".join(e[2] for e in ready),
        META: json.dumps([e[0] for e in ready], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8"),
        INFO: json.dumps({
            "model": model, "dims": DIMS, "count": len(ready),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }).encode("utf-8"),
    }
    # write all three first, then swap them in, so vectors and meta never disagree
    for path, data in files.items():
        path.with_suffix(path.suffix + ".tmp").write_bytes(data)
    for path in files:
        path.with_suffix(path.suffix + ".tmp").replace(path)
    return len(ready)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-minutes", type=float, default=20, help="stop embedding after this long")
    args = ap.parse_args()

    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        print("GEMINI_API_KEY not set — search index left unchanged")
        return
    model = os.environ.get("GEMINI_EMBED_MODEL") or "gemini-embedding-001"

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM articles WHERE published = 1 ORDER BY generated_at DESC"
        ).fetchall()

    prev = load_previous()
    entries = []      # (meta dict, text, vector or None)
    for r in rows:
        text = doc_text(r)
        h = text_hash(text)
        body = (r["your_edits"] or r["body"] or "").strip().replace("\n", " ")
        meta = {
            "id": r["id"], "h": h,
            "t": r["title"] or "", "te": r["title_en"] or "",
            "a": r["axiom"] or "", "ae": r["axiom_en"] or "",
            "d": (r["generated_at"] or "")[:10], "g": r["tag"] or "",
            "s": body[:SNIPPET_CHARS],
        }
        entries.append([meta, text, prev.get((r["id"], h))])

    todo = [e for e in entries if e[2] is None]
    print(f"{len(rows)} published, {len(rows) - len(todo)} reused, {len(todo)} to embed ({model})")

    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)  # Actions sends SIGINT, then SIGTERM
    try:
        embed_all(todo, entries, model, key, args.max_minutes)
    except KeyboardInterrupt:  # workflow cancelled: keep what we have
        print("interrupted; saving progress")
    n = write_index(entries, model)
    print(f"index: {n} of {len(rows)} articles searchable")


def embed_all(todo: list, entries: list, model: str, key: str, max_minutes: float) -> int:
    started = time.time()
    done = 0
    batch_size = BATCH
    failures = 0
    last_save = time.time()
    while done < len(todo):
        if time.time() - started > max_minutes * 60:
            print(f"time limit reached; {len(todo) - done} left for the next run")
            break
        batch = todo[done:done + batch_size]
        try:
            vecs = embed_batch([e[1] for e in batch], [e[0]["t"] for e in batch], model, key)
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:200]
            if err.code != 429 and err.code < 500:
                sys.exit(f"embedding failed: HTTP {err.code}: {detail}")
            failures += 1
            if failures > 8:
                print("  still rate-limited; writing what is done, the next run continues")
                break
            # Free-tier keys have small per-minute token quotas: shrink the batch.
            batch_size = max(5, batch_size // 2)
            wait = min(65, 5 * 2 ** min(failures, 4))
            print(f"  HTTP {err.code}; batch -> {batch_size}, waiting {wait}s: {detail}")
            time.sleep(wait)
            continue
        failures = 0
        for e, v in zip(batch, vecs):
            e[2] = quantize(v)
        done += len(batch)
        print(f"  embedded {done}/{len(todo)}")
        if time.time() - last_save > SAVE_EVERY:
            n = write_index(entries, model)
            last_save = time.time()
            print(f"  saved checkpoint: {n} articles in the index")
    return done


if __name__ == "__main__":
    main()
