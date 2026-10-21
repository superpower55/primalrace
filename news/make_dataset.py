#!/usr/bin/env python3
"""Build a LoRA training set: Fable-written commentary, fact-checked.

For each sampled article in news.db (opened read-only):
  1. Claude Fable 5.1 writes the commentary with the full system prompt
  2. factcheck.py compares it with the article text
  3. only commentaries with no fact-check issues are exported for training

The exported examples use a short system prompt instead of the ~36k-token one,
so the fine-tuned Gemma has to carry the voice and the framework itself.

Output (news/dataset/, gitignored):
  raw.jsonl       every attempt: commentary, usage, cost, fact-check result
  train.jsonl     chat-format examples ({"messages": [...]}) — clean only
  eval.jsonl      held-out clean examples (same format), never trained on
  flagged.jsonl   commentaries with fact-check issues, for review

Re-running skips articles already in raw.jsonl. Stops before --budget would
be exceeded (generation + fact-check).

Usage:
  cd news
  ANTHROPIC_API_KEY=... python3 make_dataset.py -n 200 --budget 50
  python3 make_dataset.py --export-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import threading
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import try_fable  # sets PROVIDER=anthropic before generate/llm load
import factcheck
import generate
from db import DB_PATH

OUT_DIR = Path(__file__).parent / "dataset"
RAW = OUT_DIR / "raw.jsonl"

# Reserved per article before its real cost is known (Fable + fact-check).
ESTIMATE_USD = 0.30

CULTURE_PATHS = ("/music/", "/film/", "/tv-and-radio/", "/stage/", "/culture/",
                 "/fashion/", "/style/", "/arts/", "/movies/", "/books/")

TRAIN_SYSTEM = (
    "你是费扬（Yang Fei），《原初种族》的作者。用书里的框架为下面这条新闻写一篇评论，"
    "中英双语，只用报道里有的事实。输出 JSON：title, axiom, body, title_en, axiom_en, "
    "body_en, tag, violence_layer。"
)
FIELDS = ("title", "axiom", "body", "title_en", "axiom_en", "body_en", "tag", "violence_layer")


def group_of(row: sqlite3.Row) -> str:
    url = (row["url"] or "").lower()
    if any(p in url for p in CULTURE_PATHS):
        return "culture"
    return row["tag"] or "other"


def is_eval(article_id: int, eval_frac: float) -> bool:
    h = int(hashlib.sha1(str(article_id).encode()).hexdigest(), 16)
    return (h % 1000) < eval_frac * 1000


def sample(n: int, seed: int) -> list[dict]:
    """Round-robin across groups so small topics (culture, china_tech) show up."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM articles
           WHERE length(raw_text) BETWEEN 1500 AND 15000
             AND url NOT LIKE '%/live/%'
           ORDER BY id"""
    ).fetchall()
    groups = defaultdict(list)
    for r in rows:
        groups[group_of(r)].append(dict(r))
    rng = random.Random(seed)
    for g in groups.values():
        rng.shuffle(g)
    order = sorted(groups, key=lambda g: -len(groups[g]))
    picked = []
    while len(picked) < n and any(groups.values()):
        for g in order:
            if groups[g] and len(picked) < n:
                picked.append(groups[g].pop())
    return picked


def load_raw() -> dict[int, dict]:
    if not RAW.exists():
        return {}
    return {r["id"]: r for r in (json.loads(l) for l in RAW.read_text(encoding="utf-8").splitlines() if l.strip())}


def process(article: dict, eval_frac: float, do_factcheck: bool) -> dict:
    rec = {"id": article["id"], "group": group_of(article),
           "split": "eval" if is_eval(article["id"], eval_frac) else "train"}
    try:
        c = generate.generate_commentary(article)
    except Exception as e:
        rec.update(status="error", error=str(e)[:500], cost_usd=ESTIMATE_USD)
        return rec
    rec["commentary"] = {f: c[f] for f in FIELDS}
    rec["model"] = c["model"]
    rec["gen_cost_usd"] = round(try_fable.cost_usd(c), 4)
    rec["cost_usd"] = rec["gen_cost_usd"]
    if not do_factcheck:
        rec["status"] = "unchecked"
        return rec
    try:
        fc = factcheck.check(article, rec["commentary"])
    except Exception as e:
        rec.update(status="factcheck_error", error=str(e)[:500])
        return rec
    rec["factcheck"] = fc
    rec["cost_usd"] = round(rec["cost_usd"] + fc["cost_usd"], 4)
    rec["status"] = "flagged" if fc["issues"] else "ok"
    return rec


def run(articles: list[dict], budget: float, workers: int, eval_frac: float,
        do_factcheck: bool) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    done = load_raw()
    spent = sum(r.get("cost_usd", 0) for r in done.values())
    todo = [a for a in articles if a["id"] not in done]
    print(f"{len(done)} done (${spent:.2f}), {len(todo)} to go, budget ${budget:.2f}")
    lock = threading.Lock()
    counts = defaultdict(int)

    def record(rec: dict) -> None:
        nonlocal spent
        with lock:
            spent += rec.get("cost_usd", 0)
            counts[rec["status"]] += 1
            with RAW.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            issues = len(rec.get("factcheck", {}).get("issues", []))
            print(f"#{rec['id']:<5} {rec['status']:<15} {rec['group']:<14} "
                  f"${rec.get('cost_usd', 0):.3f}  issues={issues}  total ${spent:.2f}")

    # First article alone so it writes the prompt cache; the rest read it.
    if todo and spent + ESTIMATE_USD * 2 <= budget:
        record(process(todo.pop(0), eval_frac, do_factcheck))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        running = set()
        while todo or running:
            while todo and len(running) < workers:
                if spent + ESTIMATE_USD * (len(running) + 1) > budget:
                    todo = []
                    print(f"stop: budget ${budget:.2f} reached")
                    break
                running.add(pool.submit(process, todo.pop(0), eval_frac, do_factcheck))
            if not running:
                break
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                record(fut.result())

    print(f"\n{dict(counts)}  total ${spent:.2f}")


def export() -> None:
    raw = load_raw()
    if not raw:
        print("nothing to export")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {"train": [], "eval": [], "flagged": []}
    for rec in raw.values():
        if rec["status"] == "flagged":
            out["flagged"].append({"id": rec["id"], "issues": rec["factcheck"]["issues"],
                                   "commentary": rec["commentary"]})
            continue
        if rec["status"] != "ok":
            continue
        article = dict(conn.execute("SELECT * FROM articles WHERE id=?", (rec["id"],)).fetchone())
        out[rec["split"]].append({"messages": [
            {"role": "system", "content": TRAIN_SYSTEM},
            {"role": "user", "content": generate.build_user_msg(article)},
            {"role": "assistant", "content": json.dumps(rec["commentary"], ensure_ascii=False)},
        ]})
    for name, rows in out.items():
        with (OUT_DIR / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    statuses = defaultdict(int)
    for r in raw.values():
        statuses[r["status"]] += 1
    print(f"exported train={len(out['train'])} eval={len(out['eval'])} "
          f"flagged={len(out['flagged'])}  (all statuses: {dict(statuses)})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=200, help="articles to sample (default 200)")
    ap.add_argument("--budget", type=float, default=50.0, help="USD cap across all runs (default 50)")
    ap.add_argument("--workers", type=int, default=3, help="parallel requests (default 3)")
    ap.add_argument("--eval-frac", type=float, default=0.1, help="held-out share (default 0.1)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--no-factcheck", action="store_true", help="skip the fact-check pass")
    ap.add_argument("--export-only", action="store_true")
    args = ap.parse_args()

    if not args.export_only:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("set ANTHROPIC_API_KEY (env or news/.env)")
        run(sample(args.n, args.seed), args.budget, args.workers, args.eval_frac,
            not args.no_factcheck)
    export()


if __name__ == "__main__":
    main()
