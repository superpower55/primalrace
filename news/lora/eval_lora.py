#!/usr/bin/env python3
"""Score a model on the held-out eval set against the DeepSeek reference.

Sends each eval example (short system prompt + article) to any
OpenAI-compatible endpoint — Ollama, vLLM, llama.cpp server, or DeepSeek
itself — and compares the reply with the reference answer in eval.jsonl.

Metrics (no API needed):
  json_ok           reply parses and has every field
  skip_agree        same skip / comment decision as the reference
  skip_recall       of the reference skips, how many the model skipped too
  tag_agree         same tag (comment cases where both commented)
  layer_jaccard     overlap of violence_layer sets
  ungrounded_nums   share of comments with a number (summary/body) not in the article
  summary_le_120    Chinese summary within the 120-character cap
  body_chars        median Chinese body length (reference ~1000)
  framework_terms   comments using at least one of the book's terms

Optional --judge: DeepSeek rates each comment 1–5 for voice/framework and for
faithfulness to the article, next to the reference (~$0.002 per example).

Usage:
  cd news
  # fine-tuned model served by Ollama
  python3 lora/eval_lora.py --base-url http://localhost:11434/v1 --model primal-gemma
  # base Gemma for comparison
  python3 lora/eval_lora.py --base-url http://localhost:11434/v1 --model gemma4:12b --tag base
  # with the judge
  DEEPSEEK_API_KEY=... python3 lora/eval_lora.py ... --judge
Writes news/dataset/lora/results/<tag>.jsonl and prints a summary table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_lora_dataset import FIELDS, ungrounded_numbers  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "dataset" / "lora"
TERMS = ("元暴力", "共谋", "结构暴力", "文化暴力", "直接暴力", "男性中心", "叙事", "代价", "原初种族")


def chat(base_url: str, model: str, key: str, messages: list[dict], max_tokens: int,
         json_mode: bool = False) -> str:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"] or ""
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return ""


def parse(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    a, b = text.find("{"), text.rfind("}")
    try:
        d = json.loads(text[a:b + 1])
    except Exception:
        return None
    return d if isinstance(d, dict) and all(f in d for f in FIELDS) else None


def article_text(user_msg: str) -> str:
    m = re.search(r"--- 正文 ---\n(.*)\n--- 正文结束 ---", user_msg, re.S)
    return m.group(1) if m else user_msg


JUDGE = """你在评估一个微调模型写的新闻评论。给你新闻原文、参考评论（老师模型写的）和待评评论。
打两个 1–5 分：
- voice：像不像参考评论那样用《原初种族》的框架（元暴力、共谋、谁付代价）读这条新闻，
  有观点、不空泛、不刻意硬套
- faithful：待评评论里的事实是否都来自原文（编造数字、引语、情节扣分）
只输出 JSON：{"voice": n, "faithful": n, "note": "一句话"}"""


def judge(ex: dict, got: dict, ref: dict) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    msg = (f"--- 原文 ---\n{article_text(ex['messages'][1]['content'])[:8000]}\n\n"
           f"--- 参考评论 ---\n{ref['title']}\n{ref['body']}\n\n"
           f"--- 待评评论 ---\n{got['title']}\n{got['body']}")
    out = chat("https://api.deepseek.com/v1", "deepseek-chat", key,
               [{"role": "system", "content": JUDGE}, {"role": "user", "content": msg}],
               300, json_mode=True)
    try:
        return json.loads(out)
    except Exception:
        return {}


def run_one(ex: dict, args) -> dict:
    prompt = [m for m in ex["messages"] if m["role"] != "assistant"]
    ref = json.loads(ex["messages"][-1]["content"])
    t0 = time.time()
    try:
        raw = chat(args.base_url, args.model, args.api_key, prompt, args.max_tokens)
    except Exception as e:
        raw = f"<error: {e}>"
    rec = {"id": ex["id"], "seconds": round(time.time() - t0, 1), "raw": raw, "ref": ref,
           "got": parse(raw)}
    if args.judge and rec["got"] and not rec["got"].get("skip") and not ref.get("skip"):
        rec["judge"] = judge(ex, rec["got"], ref)
    return rec


def score(recs: list[dict], examples: dict[int, dict]) -> dict:
    n = len(recs)
    ok = [r for r in recs if r["got"]]
    ref_skips = [r for r in ok if r["ref"]["skip"]]
    both = [r for r in ok if not r["ref"]["skip"] and not r["got"].get("skip")]
    comments = [r for r in ok if not r["got"].get("skip")]

    def jac(a: str, b: str) -> float:
        x = {p.strip() for p in (a or "").split(",") if p.strip()}
        y = {p.strip() for p in (b or "").split(",") if p.strip()}
        return len(x & y) / len(x | y) if x | y else 1.0

    def bad_nums(r):
        src = article_text(examples[r["id"]]["messages"][1]["content"])
        g = r["got"]
        return ungrounded_numbers(f"{g.get('summary', '')} {g.get('body', '')}", src)

    pct = lambda k, d: round(100 * k / d, 1) if d else None  # noqa: E731
    judged = [r["judge"] for r in recs if r.get("judge")]
    return {
        "n": n,
        "json_ok_%": pct(len(ok), n),
        "skip_agree_%": pct(sum(bool(r["got"]["skip"]) == r["ref"]["skip"] for r in ok), len(ok)),
        "skip_recall_%": pct(sum(bool(r["got"]["skip"]) for r in ref_skips), len(ref_skips)),
        "tag_agree_%": pct(sum(r["got"]["tag"] == r["ref"]["tag"] for r in both), len(both)),
        "layer_jaccard": round(statistics.mean(jac(r["got"]["violence_layer"], r["ref"]["violence_layer"])
                                               for r in both), 2) if both else None,
        "ungrounded_nums_%": pct(sum(bool(bad_nums(r)) for r in comments), len(comments)),
        "summary_le_120_%": pct(sum(0 < len(r["got"]["summary"]) <= 120 for r in comments), len(comments)),
        "body_chars_median": statistics.median(len(r["got"]["body"]) for r in comments) if comments else None,
        "framework_terms_%": pct(sum(any(t in r["got"]["body"] for t in TERMS) for r in comments),
                                 len(comments)),
        "judge_voice": round(statistics.mean(j.get("voice", 0) for j in judged), 2) if judged else None,
        "judge_faithful": round(statistics.mean(j.get("faithful", 0) for j in judged), 2) if judged else None,
        "sec_median": statistics.median(r["seconds"] for r in recs) if recs else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="OpenAI-compatible base, e.g. http://localhost:11434/v1")
    ap.add_argument("--model")
    ap.add_argument("--api-key", default=os.environ.get("EVAL_API_KEY", ""))
    ap.add_argument("--tag", default="", help="name for the results file (default: model)")
    ap.add_argument("--data", default=str(DATA / "eval.jsonl"))
    ap.add_argument("-n", type=int, default=0, help="only the first n examples")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--judge", action="store_true", help="DeepSeek rates voice + faithfulness")
    ap.add_argument("--reference", action="store_true",
                    help="score the reference answers themselves (sanity check, no model calls)")
    args = ap.parse_args()

    examples = [json.loads(l) for l in Path(args.data).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.n:
        examples = examples[:args.n]
    by_id = {e["id"]: e for e in examples}

    if args.reference:
        recs = [{"id": e["id"], "seconds": 0, "ref": json.loads(e["messages"][-1]["content"]),
                 "got": json.loads(e["messages"][-1]["content"])} for e in examples]
        tag = "reference"
    else:
        if not (args.base_url and args.model):
            ap.error("--base-url and --model are required (or --reference)")
        tag = args.tag or re.sub(r"[^\w.-]+", "_", args.model)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            recs = []
            for i, r in enumerate(pool.map(lambda e: run_one(e, args), examples), 1):
                recs.append(r)
                state = "skip" if r["got"] and r["got"].get("skip") else ("ok" if r["got"] else "BAD JSON")
                print(f"[{i}/{len(examples)}] #{r['id']} {state} {r['seconds']}s", flush=True)

    out = DATA / "results"
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{tag}.jsonl").open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = score(recs, by_id)
    (out / f"{tag}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n== {tag} ==")
    for k, v in summary.items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
