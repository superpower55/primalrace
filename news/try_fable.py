#!/usr/bin/env python3
"""Small paid trial: rewrite a few existing articles with Claude Fable 5.1 and
compare them side by side with the current Gemma commentary.

news.db is opened read-only — nothing on the site changes. Results go to
news/fable_trial/ (gitignored):
  results.jsonl   one line per article (Fable output + usage + cost)
  compare.html    Gemma vs Fable, side by side

Re-running skips articles already in results.jsonl, so an interrupted run
never pays twice. Stops before the next article would push the total past
--budget.

Usage:
  cd news
  ANTHROPIC_API_KEY=sk-ant-... python3 try_fable.py              # 10 articles, $4 cap
  python3 try_fable.py -n 5 --budget 2
  python3 try_fable.py --ids 4512 4890 5011
  python3 try_fable.py --html-only                                # rebuild compare.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Must be set before llm.py is imported (it reads PROVIDER at import time).
os.environ["PROVIDER"] = "anthropic"

import generate  # noqa: E402
from db import DB_PATH  # noqa: E402

OUT_DIR = Path(__file__).parent / "fable_trial"
RESULTS = OUT_DIR / "results.jsonl"
COMPARE = OUT_DIR / "compare.html"

# Claude Fable 5.1, USD per million tokens. A fallback model, if one serves a
# request, is cheaper — so these prices give an upper bound.
PRICE_INPUT = 10.00
PRICE_CACHE_WRITE = 12.50   # 5-minute TTL
PRICE_CACHE_READ = 0.25
PRICE_OUTPUT = 50.00        # includes thinking tokens
FIRST_ARTICLE_ESTIMATE = 0.60  # before any real cost is observed (cold cache)


def cost_usd(r: dict) -> float:
    return (r["input_tokens"] * PRICE_INPUT
            + r["cache_create_tokens"] * PRICE_CACHE_WRITE
            + r["cache_read_tokens"] * PRICE_CACHE_READ
            + r["output_tokens"] * PRICE_OUTPUT) / 1_000_000


def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_articles(n: int, seed: int) -> list[sqlite3.Row]:
    """Spread the picks across tags so one hot topic doesn't dominate."""
    with connect_ro() as conn:
        rows = conn.execute(
            """SELECT * FROM articles
               WHERE body != '' AND body IS NOT NULL
                 AND length(raw_text) BETWEEN 1500 AND 12000
               ORDER BY id"""
        ).fetchall()
    by_tag = defaultdict(list)
    for r in rows:
        by_tag[r["tag"] or "other"].append(r)
    rng = random.Random(seed)
    for group in by_tag.values():
        rng.shuffle(group)
    tags = sorted(by_tag, key=lambda t: -len(by_tag[t]))
    picked = []
    while len(picked) < n and any(by_tag.values()):
        for t in tags:
            if by_tag[t] and len(picked) < n:
                picked.append(by_tag[t].pop())
    return picked


def load_by_ids(ids: list[int]) -> list[sqlite3.Row]:
    with connect_ro() as conn:
        q = f"SELECT * FROM articles WHERE id IN ({','.join('?' * len(ids))})"
        return conn.execute(q, ids).fetchall()


def load_results() -> dict[int, dict]:
    if not RESULTS.exists():
        return {}
    out = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = r
    return out


def run(articles: list[sqlite3.Row], budget: float) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    done = load_results()
    spent = sum(r.get("cost_usd", 0) for r in done.values())
    costs = [r["cost_usd"] for r in done.values() if r.get("cost_usd")]
    print(f"already spent ${spent:.2f} on {len(done)} article(s); budget ${budget:.2f}")

    # Keep the raw usage even when the reply can't be parsed — it was billed.
    last = {}
    real_llm_generate = generate.llm_generate

    def capturing_llm_generate(*a, **kw):
        last["result"] = real_llm_generate(*a, **kw)
        return last["result"]

    generate.llm_generate = capturing_llm_generate

    for a in articles:
        if a["id"] in done:
            continue
        estimate = max(costs) if costs else FIRST_ARTICLE_ESTIMATE
        if spent + estimate > budget:
            print(f"stop: next article could cost ~${estimate:.2f}, "
                  f"would pass the ${budget:.2f} budget")
            break

        print(f"#{a['id']} {a['source_title'][:70]}")
        last.clear()
        record = {"id": a["id"]}
        try:
            record.update(generate.generate_commentary(dict(a)))
        except Exception as e:
            record["error"] = str(e)[:500]
            print(f"  [error] {record['error'][:200]}")
        usage = last.get("result")
        if usage:
            record["cost_usd"] = round(cost_usd(usage), 4)
            spent += record["cost_usd"]
            costs.append(record["cost_usd"])
            print(f"  ${record['cost_usd']:.3f}  (total ${spent:.2f})  "
                  f"in={usage['input_tokens']} cache_w={usage['cache_create_tokens']} "
                  f"cache_r={usage['cache_read_tokens']} out={usage['output_tokens']}")
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\ntotal ${spent:.2f}")


# ------------------------ compare page ---------------------------------------

def _paras(text: str) -> str:
    return "".join(f"<p>{html.escape(p)}</p>" for p in (text or "").split("\n") if p.strip())


def _column(label: str, c: dict) -> str:
    if c.get("error"):
        return f'<div class="col"><h3>{label}</h3><p class="err">{html.escape(c["error"])}</p></div>'
    return f"""<div class="col">
  <h3>{label}</h3>
  <h4>{html.escape(c.get("title") or "")}</h4>
  <blockquote>{html.escape(c.get("axiom") or "")}</blockquote>
  {_paras(c.get("body"))}
  <details><summary>English</summary>
    <h4>{html.escape(c.get("title_en") or "")}</h4>
    <blockquote>{html.escape(c.get("axiom_en") or "")}</blockquote>
    {_paras(c.get("body_en"))}
  </details>
  <p class="meta">{html.escape(c.get("model") or "")} · {html.escape(c.get("tag") or "")} · {html.escape(c.get("violence_layer") or "")}</p>
</div>"""


def write_compare() -> None:
    results = load_results()
    if not results:
        print("no results yet")
        return
    articles = {a["id"]: a for a in load_by_ids(list(results))}
    total = sum(r.get("cost_usd", 0) for r in results.values())
    sections = []
    for aid, fable in results.items():
        a = articles.get(aid)
        if a is None:
            continue
        sections.append(f"""<section>
  <h2><a href="{html.escape(a["url"])}">{html.escape(a["source_title"] or a["url"])}</a></h2>
  <p class="meta">#{aid} · {html.escape(a["source"] or "")} · Fable cost ${fable.get("cost_usd", 0):.3f}</p>
  <div class="cols">{_column("Gemma", dict(a))}{_column("Fable", fable)}</div>
</section>""")
    COMPARE.write_text(f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gemma vs Fable</title>
<style>
  body {{ font: 16px/1.7 -apple-system, "PingFang SC", sans-serif; max-width: 1200px; margin: 0 auto; padding: 16px; color: #1a1109; background: #faf7f2; }}
  section {{ border-top: 2px solid #8b1a1a; margin-top: 32px; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 760px) {{ .cols {{ grid-template-columns: 1fr; }} }}
  .col h3 {{ color: #8b1a1a; margin-bottom: 0; }}
  blockquote {{ margin: 8px 0; padding-left: 12px; border-left: 3px solid #c9b79c; font-style: italic; }}
  .meta {{ color: #6b5c47; font-size: 13px; }}
  .err {{ color: #b00020; }}
  a {{ color: inherit; }}
</style></head><body>
<h1>Gemma vs Fable</h1>
<p class="meta">{len(results)} articles · Fable total ${total:.2f}</p>
{"".join(sections)}
</body></html>""", encoding="utf-8")
    print(f"wrote {COMPARE}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=10, help="number of articles (default 10)")
    ap.add_argument("--budget", type=float, default=4.0, help="USD cap across all runs (default 4)")
    ap.add_argument("--ids", type=int, nargs="+", help="specific article ids instead of a sample")
    ap.add_argument("--seed", type=int, default=7, help="sampling seed")
    ap.add_argument("--html-only", action="store_true", help="only rebuild compare.html")
    args = ap.parse_args()

    if not args.html_only:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("set ANTHROPIC_API_KEY (env or news/.env)")
        articles = load_by_ids(args.ids) if args.ids else pick_articles(args.n, args.seed)
        run(articles, args.budget)
    write_compare()


if __name__ == "__main__":
    main()
