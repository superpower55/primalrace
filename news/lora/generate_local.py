#!/usr/bin/env python3
"""Generate eval replies on the GPU box with the base model + LoRA adapter.

For machines without Ollama/vLLM (Kaggle, Colab). Writes one line per example,
{id, raw, seconds}, which eval_lora.py scores with --replies.

Usage:
  python3 news/lora/generate_local.py --adapter news/lora/out/adapter -n 20 \
      --out news/dataset/lora/results/lora.replies.jsonl
  python3 news/lora/generate_local.py -n 10 --out .../base.replies.jsonl   # no adapter = base
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from train_lora import BASE, DATA, as_turns, text_tokenizer


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--adapter", default="", help="LoRA adapter dir (omit for the base model)")
    ap.add_argument("--data", default=str(DATA / "eval.jsonl"))
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--max-new", type=int, default=3000)
    ap.add_argument("--max-seq", type=int, default=8192)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from unsloth import FastModel
    model, tok = FastModel.from_pretrained(args.adapter or args.base,
                                           max_seq_length=args.max_seq, load_in_4bit=True)
    FastModel.for_inference(model)
    t = text_tokenizer(tok)

    rows = [json.loads(l) for l in Path(args.data).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    for i, r in enumerate(rows[:args.n], 1):
        if r["id"] in done:
            continue
        prompt_turns, _ = as_turns(r["messages"])
        prompt = t.apply_chat_template(prompt_turns, tokenize=False, add_generation_prompt=True)
        ids = t(prompt, add_special_tokens=False, return_tensors="pt").to(model.device)
        t0 = time.time()
        gen = model.generate(**ids, max_new_tokens=args.max_new, do_sample=True,
                             temperature=0.3, top_p=0.95)
        raw = t.decode(gen[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
        sec = round(time.time() - t0, 1)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": r["id"], "raw": raw, "seconds": sec}, ensure_ascii=False) + "\n")
        print(f"[{i}/{min(args.n, len(rows))}] #{r['id']} {sec}s {raw[:80]!r}", flush=True)


if __name__ == "__main__":
    main()
