#!/usr/bin/env python3
"""Write the neutral news summary for published articles that predate it.

Uses the same summary rules as the commentary prompt (prompts.system.
SUMMARY_FIELDS) and the configured LLM provider. Run a few per pipeline run
(cron_pipeline.py) so the backlog clears over a day or two without a long job.

By default only DeepSeek-written articles are filled — they are the LoRA
training set and need the same JSON shape as new articles. Set
SUMMARY_BACKFILL_ALL=1 to continue with every other published article.

Usage:
  cd news
  python3 backfill_summaries.py --limit 50
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from db import connect  # noqa: E402
from generate import _parse_json_objects, _strip_json_fence  # noqa: E402
from llm import generate as llm_generate  # noqa: E402
from prompts.system import SUMMARY_FIELDS  # noqa: E402

SYSTEM = [{"type": "text", "text": f"""你为一个新闻评论网站写新闻摘要，放在评论正文之前，
让读者先知道发生了什么。你只有这一篇报道：数字、日期、引语、人物都只用报道原文里有的。

输出一个 JSON 对象，只有两个字段：

{SUMMARY_FIELDS}

中文里的引号用「」或“”，不要用英文直引号。只输出这个 JSON 对象。"""}]

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "summary_en": {"type": "string"}},
    "required": ["summary", "summary_en"],
    "additionalProperties": False,
}


def summarize(source_title: str, raw_text: str) -> tuple[str, str]:
    msg = f"标题：{source_title}\n\n--- 正文 ---\n{raw_text[:15000]}\n--- 正文结束 ---"
    for attempt in (1, 2):
        result = llm_generate(SYSTEM, msg, max_tokens=800, json_schema=SCHEMA)
        try:
            d = _parse_json_objects(_strip_json_fence(result["text"]))
            return (d.get("summary") or "").strip(), (d.get("summary_en") or "").strip()
        except json.JSONDecodeError:
            if attempt == 2:
                raise


def backfill(limit: int) -> int:
    only_deepseek = os.environ.get("SUMMARY_BACKFILL_ALL") != "1"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, source_title, raw_text FROM articles
                WHERE published = 1 AND (summary IS NULL OR summary = '')
                  AND raw_text IS NOT NULL AND raw_text != ''
                  {"AND model = 'deepseek-chat'" if only_deepseek else ""}
                ORDER BY generated_at DESC LIMIT ?""", (limit,)).fetchall()
    done = 0
    for r in rows:
        try:
            zh, en = summarize(r["source_title"] or "", r["raw_text"])
        except Exception as e:
            print(f"  [summary error] #{r['id']}: {str(e)[:120]}", file=sys.stderr)
            continue
        if zh:
            with connect() as conn:
                conn.execute("UPDATE articles SET summary = ?, summary_en = ? WHERE id = ?",
                             (zh, en, r["id"]))
            done += 1
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    print(f"summaries written: {backfill(args.limit)}")


if __name__ == "__main__":
    main()
