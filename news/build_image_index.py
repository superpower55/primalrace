#!/usr/bin/env python3
"""Build the CLIP index for search by image (served by api/image-search.js).

Uses the quantized CLIP ViT-B/32 ONNX models from Xenova/clip-vit-base-patch32
— the same files the /news/ page runs in the browser (transformers.js) to embed
a searcher's photo, so query and index vectors come from one model. Free: no
API, runs on the GitHub Actions CPU.

Steps per run (each resumable):
  1. find the lead image (og:image) of each published article that has no
     articles.image_url (new articles store theirs when fetched)
     -> news/search/clip/found.json  {id: url, or '' when none / unreachable}
     Kept out of news.db so this workflow never commits the database the
     news workflow writes every 10 minutes.
  2. CLIP image vectors for those images   -> news/search/clip/img.{bin,json}
     ("articles with a similar image")
  3. CLIP text vectors for each article's original headline
                                            -> news/search/clip/txt.{bin,json}
     ("articles about this image"; CLIP's text side is English, and the
     headline describes the news itself)

Usage:
  cd news
  python3 build_image_index.py --max-fetch 1500 --max-minutes 60
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from build_search_index import quantize
from db import connect
from generate import USER_AGENT

MODEL = "Xenova/clip-vit-base-patch32"
VISION_FILE = "onnx/vision_model_quantized.onnx"   # transformers.js dtype "q8"
TEXT_FILE = "onnx/text_model_quantized.onnx"
DIMS = 512
OUT_DIR = Path(__file__).parent / "search" / "clip"
FOUND = OUT_DIR / "found.json"
MAX_IMAGE_BYTES = 8_000_000
BATCH = 16

OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


# ------------------------------------------------------------ 1. find images

def find_image(url: str) -> str:
    # Same request as generate.fetch_article: several sites (CNBC, Billboard,
    # Deadline) refuse a bare urllib request but serve this one.
    try:
        resp = requests.get(url, timeout=25, allow_redirects=True, headers={
            **HEADERS, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        if resp.status_code >= 400:
            return ""
        html = resp.text[:2_000_000]  # some sites put og:image ~600k chars in
    except Exception:
        return ""
    m = OG_IMAGE.search(html)
    return (m.group(1) or m.group(2)).replace("&amp;", "&").strip() if m else ""


def load_found() -> dict[int, str]:
    if FOUND.exists():
        return {int(k): v for k, v in json.loads(FOUND.read_text(encoding="utf-8")).items()}
    img = OUT_DIR / "img.json"  # first run after the move: seed from the index
    if img.exists():
        return {m["id"]: m["img"] for m in json.loads(img.read_text(encoding="utf-8"))}
    return {}


def save_found(found: dict[int, str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FOUND.write_text(json.dumps({str(k): v for k, v in sorted(found.items())},
                                separators=(",", ":")), encoding="utf-8")


def step_find_images(found: dict[int, str], max_fetch: int, deadline: float) -> None:
    with connect() as conn:
        rows = [r for r in conn.execute(
            """SELECT id, url FROM articles WHERE published = 1 AND image_url IS NULL
               ORDER BY generated_at DESC""") if r["id"] not in found][:max_fetch]
    print(f"[1/3] finding lead images for {len(rows)} articles")
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, len(rows), 40):
            if time.time() > deadline:
                print("  time limit reached")
                break
            chunk = rows[i:i + 40]
            for r, img in zip(chunk, pool.map(lambda r: find_image(r["url"]), chunk)):
                found[r["id"]] = img
            done += len(chunk)
    save_found(found)
    print(f"  {done} checked")


# ------------------------------------------------------------ CLIP models

class Clip:
    def __init__(self) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import CLIPImageProcessor, CLIPTokenizer
        self.processor = CLIPImageProcessor.from_pretrained(MODEL)
        self.tokenizer = CLIPTokenizer.from_pretrained(MODEL)
        self.vision = ort.InferenceSession(hf_hub_download(MODEL, VISION_FILE))
        self.text = ort.InferenceSession(hf_hub_download(MODEL, TEXT_FILE))

    def images(self, pil_images: list) -> list:
        px = self.processor(images=pil_images, return_tensors="np")["pixel_values"]
        return list(self.vision.run(None, {"pixel_values": px})[0])

    def texts(self, texts: list[str]) -> list:
        import numpy as np
        t = self.tokenizer(texts, padding="max_length", truncation=True,
                           max_length=77, return_tensors="np")
        return list(self.text.run(None, {"input_ids": t["input_ids"].astype(np.int64)})[0])


def load_image(url: str):
    from PIL import Image
    try:
        resp = requests.get(url, timeout=30, headers={**HEADERS, "Accept": "image/*"}, stream=True)
        if resp.status_code >= 400:
            return "dead"
        data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        if len(data) > MAX_IMAGE_BYTES:
            return "dead"
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None  # network hiccup: try again next run


# ------------------------------------------------------------ index files

def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def load(name: str) -> dict[tuple[int, str], bytes]:
    meta_f, vec_f = OUT_DIR / f"{name}.json", OUT_DIR / f"{name}.bin"
    if not (meta_f.exists() and vec_f.exists()):
        return {}
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    raw = vec_f.read_bytes()
    if len(raw) != len(meta) * DIMS:
        return {}
    return {(m["id"], m["h"]): raw[i * DIMS:(i + 1) * DIMS] for i, m in enumerate(meta)}


def save(name: str, entries: list) -> int:
    ready = [e for e in entries if e[1] is not None]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        OUT_DIR / f"{name}.bin": b"".join(e[1] for e in ready),
        OUT_DIR / f"{name}.json": json.dumps([e[0] for e in ready], ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8"),
    }
    for path, data in files.items():
        path.with_suffix(path.suffix + ".tmp").write_bytes(data)
    for path in files:
        path.with_suffix(path.suffix + ".tmp").replace(path)
    return len(ready)


# ------------------------------------------------------------ 2 + 3

def step_images(clip: Clip, rows, found: dict[int, str], deadline: float) -> int:
    prev = load("img")
    entries = []
    for r in rows:
        url = r["image_url"] or found.get(r["id"], "")
        if not url.startswith("http"):
            continue
        meta = {"id": r["id"], "h": h(url), "img": url,
                "t": r["title"] or "", "te": r["title_en"] or "",
                "d": (r["generated_at"] or "")[:10]}
        entries.append([meta, prev.get((r["id"], meta["h"]))])
    todo = [e for e in entries if e[1] is None]
    print(f"[2/3] {len(entries)} articles with an image, {len(todo)} to embed")
    dead = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for i in range(0, len(todo), BATCH):
            if time.time() > deadline:
                print("  time limit reached")
                break
            batch = todo[i:i + BATCH]
            imgs = list(pool.map(lambda e: load_image(e[0]["img"]), batch))
            ok = [(e, im) for e, im in zip(batch, imgs) if im not in (None, "dead")]
            dead += [e[0]["id"] for e, im in zip(batch, imgs) if im == "dead"]
            if ok:
                for (e, _), v in zip(ok, clip.images([im for _, im in ok])):
                    e[1] = quantize(list(v))
            if (i // BATCH) % 25 == 0:
                print(f"  {min(i + BATCH, len(todo))}/{len(todo)}")
    for i in dead:  # image gone or refused: stop trying it
        found[i] = ""
    save_found(found)
    return save("img", entries)


def step_texts(clip: Clip, rows) -> int:
    prev = load("txt")
    entries = []
    for r in rows:
        text = (r["source_title"] or r["title_en"] or "").strip()
        if not text:
            continue
        meta = {"id": r["id"], "h": h(text), "t": r["title"] or "", "te": r["title_en"] or "",
                "a": r["axiom"] or "", "ae": r["axiom_en"] or "",
                "d": (r["generated_at"] or "")[:10], "g": r["tag"] or ""}
        entries.append([meta, prev.get((r["id"], meta["h"])), text])
    todo = [e for e in entries if e[1] is None]
    print(f"[3/3] {len(entries)} headlines, {len(todo)} to embed")
    for i in range(0, len(todo), 64):
        batch = todo[i:i + 64]
        for e, v in zip(batch, clip.texts([e[2] for e in batch])):
            e[1] = quantize(list(v))
    return save("txt", [e[:2] for e in entries])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-fetch", type=int, default=1500, help="articles to look up images for")
    ap.add_argument("--max-minutes", type=float, default=60)
    args = ap.parse_args()
    start = time.time()
    deadline = start + args.max_minutes * 60

    found = load_found()
    step_find_images(found, args.max_fetch, start + args.max_minutes * 60 * 0.3)

    with connect() as conn:
        rows = conn.execute(
            """SELECT id, image_url, source_title, title, title_en, axiom, axiom_en, tag, generated_at
               FROM articles WHERE published = 1 ORDER BY generated_at DESC""").fetchall()
    clip = Clip()
    n_img = step_images(clip, rows, found, deadline)
    n_txt = step_texts(clip, rows)
    (OUT_DIR / "info.json").write_text(json.dumps({
        "model": MODEL, "dims": DIMS, "images": n_img, "texts": n_txt,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }), encoding="utf-8")
    print(f"clip index: {n_img} images, {n_txt} headlines")


if __name__ == "__main__":
    sys.exit(main())
