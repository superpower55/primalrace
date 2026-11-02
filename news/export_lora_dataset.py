#!/usr/bin/env python3
"""Export the LoRA distillation set from news.db: DeepSeek commentary -> Gemma 4 12B.

No API calls. Every article DeepSeek wrote with the full ~36k-token prompt
becomes one chat example whose system prompt is the short TRAIN_SYSTEM, so the
fine-tuned model has to carry the voice and the framework itself:

  system     prompts.system.TRAIN_SYSTEM
  user       prompts.system.build_user_msg(article), article text cut to --max-chars
  assistant  the JSON DeepSeek returned (same fields, same order)

Included:
  - published DeepSeek articles that have the news summary (older ones get
    theirs from backfill_summaries.py, 20 per pipeline run)
  - DeepSeek's skip decisions (tag='skipped', reason in axiom) so the model
    also learns when not to comment

Dropped (counted in stats.json): missing fields, body outside length bounds,
a number in the summary that is not in the article, article text too short.

The split is fixed by article id (sha1), so an article never moves between
train and eval as the set grows.

Output (news/dataset/lora/, gitignored):
  train.jsonl  eval.jsonl   {"id", "messages": [...]}
  stats.json

Usage:
  cd news
  python3 export_lora_dataset.py
  python3 export_lora_dataset.py --eval-frac 0.1 --max-chars 12000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from db import DB_PATH

from prompts.system import TRAIN_SYSTEM, build_user_msg

OUT_DIR = Path(__file__).parent / "dataset" / "lora"
FIELDS = ("summary", "summary_en", "title", "axiom", "body", "title_en", "axiom_en", "body_en",
          "tag", "violence_layer", "skip", "skip_reason")
TAGS = {"china_tech", "gender", "international", "philosophy", "tech", "culture", "good_news", "other"}
LAYERS = ("direct", "structural", "cultural", "meta")
TEACHER = "deepseek-chat"
NUM = re.compile(r"\d+(?:[.,]\d+)*")


def is_eval(article_id: int, frac: float) -> bool:
    return int(hashlib.sha1(str(article_id).encode()).hexdigest(), 16) % 1000 < frac * 1000


def norm_layers(v: str | None) -> str:
    """'meta, cultural' -> 'cultural,meta' (one spelling per set of layers)."""
    got = {p.strip() for p in (v or "").split(",") if p.strip()}
    return ",".join(l for l in LAYERS if l in got)


def _forms(num: str, unit: str) -> set[str]:
    """Ways the article may have written a number: 330万 -> 3.3 (million), 3300000."""
    forms = {num}
    if unit in ("万", "亿"):
        v = float(num) * (1e4 if unit == "万" else 1e8)
        forms |= {f"{v / d:g}" for d in (1, 1e3, 1e6, 1e9)}
    return forms


def ungrounded_numbers(text: str, source: str) -> list[str]:
    """Numbers (2+ digits) in text that never appear in the source article."""
    src = source.replace(",", "")
    bad = []
    for m in NUM.finditer(text):
        n = m.group().replace(",", "")
        if len(n.replace(".", "")) < 2:
            continue
        unit = text[m.end():m.end() + 1]
        if not any(f in src for f in _forms(n, unit)):
            bad.append(m.group() + unit)
    return bad


def commentary_of(r: sqlite3.Row) -> dict:
    if r["tag"] == "skipped":
        return {**{f: "" for f in FIELDS}, "tag": "other", "skip": True,
                "skip_reason": (r["axiom"] or "").strip()}
    c = {f: (r[f] or "").strip() for f in FIELDS[:9]}
    c.update(violence_layer=norm_layers(r["violence_layer"]), skip=False, skip_reason="")
    return c


def reject_reason(r: sqlite3.Row, c: dict, min_chars: int) -> str | None:
    if len(r["raw_text"] or "") < min_chars:
        return "article_too_short"
    if c["skip"]:
        return None if c["skip_reason"] else "skip_without_reason"
    missing = [f for f in FIELDS[:8] if not c[f]]
    if missing:
        return "no_summary" if missing[0] in ("summary", "summary_en") else "missing_field"
    if c["tag"] not in TAGS:
        return "bad_tag"
    if not 300 <= len(c["body"]) <= 3000:
        return "body_length"
    if ungrounded_numbers(c["summary"] + " " + c["summary_en"], r["raw_text"]):
        return "summary_number_not_in_article"
    return None


def export(eval_frac: float, max_chars: int, min_chars: int) -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM articles
           WHERE model = ? AND (published = 1 OR tag = 'skipped')
           ORDER BY id""", (TEACHER,)).fetchall()

    out = {"train": [], "eval": []}
    stats = Counter()
    tags = Counter()
    for r in rows:
        c = commentary_of(r)
        why = reject_reason(r, c, min_chars)
        if why:
            stats["dropped:" + why] += 1
            continue
        article = dict(r)
        text = article["raw_text"]
        if len(text) > max_chars:  # cut at a paragraph break where possible
            cut = text.rfind("\n", 0, max_chars)
            article["raw_text"] = text[:cut if cut > max_chars * 0.8 else max_chars] + "\n[……]"
        split = "eval" if is_eval(r["id"], eval_frac) else "train"
        out[split].append({"id": r["id"], "messages": [
            {"role": "system", "content": TRAIN_SYSTEM},
            {"role": "user", "content": build_user_msg(article)},
            {"role": "assistant", "content": json.dumps({f: c[f] for f in FIELDS}, ensure_ascii=False)},
        ]})
        stats[f"{split}:{'skip' if c['skip'] else 'comment'}"] += 1
        tags["skip" if c["skip"] else c["tag"]] += 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, items in out.items():
        with (OUT_DIR / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    chars = [sum(len(m["content"]) for m in it["messages"]) for it in out["train"] + out["eval"]]
    result = {
        "teacher": TEACHER, "candidates": len(rows),
        "train": len(out["train"]), "eval": len(out["eval"]),
        "counts": dict(sorted(stats.items())), "tags": dict(tags.most_common()),
        "max_example_chars": max(chars, default=0),
        "avg_example_chars": round(sum(chars) / len(chars)) if chars else 0,
        "eval_frac": eval_frac, "max_article_chars": max_chars,
    }
    (OUT_DIR / "stats.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-frac", type=float, default=0.1)
    ap.add_argument("--max-chars", type=int, default=12000, help="article text cut (default 12000)")
    ap.add_argument("--min-chars", type=int, default=600, help="drop shorter articles (default 600)")
    args = ap.parse_args()
    print(json.dumps(export(args.eval_frac, args.max_chars, args.min_chars), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
