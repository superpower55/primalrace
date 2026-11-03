#!/usr/bin/env python3
"""QLoRA fine-tune of Gemma 4 12B on the DeepSeek commentary set (Unsloth).

Runs on a rented GPU (24 GB is enough at 4-bit; an L40S 48 GB or A100 is
comfortable). Input is what export_lora_dataset.py writes:

  news/dataset/lora/train.jsonl   news/dataset/lora/eval.jsonl

Loss is only on the assistant JSON: the system + user part of each example is
masked out (labels -100). The prompt is the chat template's generation prompt,
exactly what Ollama / vLLM send at inference (Gemma 4 appends an empty
thinking block there).
Examples longer than --max-seq are dropped, never truncated — a cut-off JSON
would teach the model to stop mid-object.

Usage (on the GPU box, from the repo root):
  pip install -r news/lora/requirements.txt
  python3 news/lora/train_lora.py                         # train, save adapter
  python3 news/lora/train_lora.py --gguf q4_k_m           # + GGUF for Ollama
  python3 news/lora/train_lora.py --check-only            # tokenize + mask, no GPU
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dataset" / "lora"
BASE = "unsloth/gemma-4-12b-it"


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def as_turns(messages: list[dict]) -> tuple[list[dict], str]:
    """[system, user, assistant] -> (prompt turns, assistant answer)."""
    return [m for m in messages if m["role"] != "assistant"], \
        next(m["content"] for m in messages if m["role"] == "assistant")


def text_tokenizer(tok):
    """Gemma's Unsloth loader returns a processor; the text tokenizer sits inside."""
    return getattr(tok, "tokenizer", tok)


def encode(tok, messages: list[dict], max_seq: int) -> dict | None:
    """input_ids + labels with everything before the answer masked; None if too long."""
    t = text_tokenizer(tok)
    prompt_turns, answer = as_turns(messages)
    # Train on exactly what inference sees: the generation prompt (Gemma 4 adds
    # an empty thinking block there) + the answer + the template's end of turn.
    prompt = t.apply_chat_template(prompt_turns, tokenize=False, add_generation_prompt=True)
    rendered = t.apply_chat_template(prompt_turns + [{"role": "assistant", "content": answer}],
                                     tokenize=False)
    at = rendered.rfind(answer)
    if at < 0:
        raise ValueError("chat template does not contain the answer verbatim")
    full = prompt + answer + rendered[at + len(answer):]
    # The rendered text already carries <bos>; don't let the tokenizer add another.
    p_ids = t(prompt, add_special_tokens=False)["input_ids"]
    f_ids = t(full, add_special_tokens=False)["input_ids"]
    if f_ids[:len(p_ids)] != p_ids:
        raise ValueError("prompt tokens differ inside the full example")
    if len(f_ids) > max_seq:
        return None
    labels = [-100] * len(p_ids) + f_ids[len(p_ids):]
    return {"input_ids": f_ids, "attention_mask": [1] * len(f_ids), "labels": labels}


def build(tok, rows: list[dict], max_seq: int) -> tuple[list[dict], int]:
    out, dropped = [], 0
    for r in rows:
        e = encode(tok, r["messages"], max_seq)
        if e is None:
            dropped += 1
        else:
            out.append(e)
    return out, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default=str(ROOT / "lora" / "out"))
    ap.add_argument("--max-seq", type=int, default=8192)
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--save-steps", type=int, default=50)
    ap.add_argument("--resume", action="store_true", help="continue from the last checkpoint in --out")
    ap.add_argument("--gguf", default="", help="also export GGUF, e.g. q4_k_m (for Ollama)")
    ap.add_argument("--check-only", action="store_true",
                    help="load only the tokenizer, encode the set, print stats")
    args = ap.parse_args()
    data = Path(args.data)
    train_rows, eval_rows = load(data / "train.jsonl"), load(data / "eval.jsonl")

    if args.check_only:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(args.base)
        tr, d1 = build(tok, train_rows, args.max_seq)
        ev, d2 = build(tok, eval_rows, args.max_seq)
        lens = sorted(len(e["input_ids"]) for e in tr + ev)
        ans = [sum(l != -100 for l in e["labels"]) for e in tr + ev]
        print(f"train {len(tr)} (+{d1} too long)  eval {len(ev)} (+{d2} too long)")
        if lens:
            print(f"tokens/example: median {lens[len(lens) // 2]}, max {lens[-1]}; "
                  f"answer tokens median {sorted(ans)[len(ans) // 2]}")
            e = tr[0] if tr else ev[0]
            t = text_tokenizer(tok)
            first = next(i for i, l in enumerate(e["labels"]) if l != -100)
            print("--- end of masked prompt ---\n" + t.decode(e["input_ids"][first - 12:first]))
            print("--- start of trained answer ---\n" + t.decode(e["input_ids"][first:first + 24]))
        return

    from unsloth import FastModel  # first: it patches transformers
    import torch
    from datasets import Dataset
    from transformers import DataCollatorForSeq2Seq, Trainer, TrainingArguments

    model, tok = FastModel.from_pretrained(
        args.base, max_seq_length=args.max_seq, load_in_4bit=True, full_finetuning=False)
    model = FastModel.get_peft_model(
        model,
        finetune_vision_layers=False, finetune_language_layers=True,
        finetune_attention_modules=True, finetune_mlp_modules=True,
        r=args.rank, lora_alpha=args.rank, lora_dropout=0, bias="none", random_state=3407,
        use_gradient_checkpointing="unsloth")  # long examples on a 16 GB card

    tr, d1 = build(tok, train_rows, args.max_seq)
    ev, d2 = build(tok, eval_rows, args.max_seq)
    print(f"train {len(tr)} examples ({d1} too long, dropped), eval {len(ev)} ({d2} dropped)")

    t = text_tokenizer(tok)
    if t.pad_token is None:
        t.pad_token = t.eos_token
    bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    trainer = Trainer(
        model=model,
        train_dataset=Dataset.from_list(tr),
        eval_dataset=Dataset.from_list(ev) if ev else None,
        data_collator=DataCollatorForSeq2Seq(t, padding=True, label_pad_token_id=-100),
        args=TrainingArguments(
            output_dir=args.out,
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            weight_decay=0.01,
            optim="adamw_8bit",
            bf16=bf16, fp16=not bf16,
            logging_steps=5,
            eval_strategy="epoch" if ev else "no",
            save_strategy="steps",
            save_steps=args.save_steps,  # Kaggle/Colab sessions can end; resume with --resume
            save_total_limit=2,
            report_to="none",
            seed=3407,
            remove_unused_columns=False,
        ),
    )
    resume = args.resume and any(Path(args.out).glob("checkpoint-*"))
    trainer.train(resume_from_checkpoint=True if resume else None)

    adapter = Path(args.out) / "adapter"
    model.save_pretrained(str(adapter))
    tok.save_pretrained(str(adapter))
    print(f"adapter saved to {adapter}")
    if args.gguf:
        gguf = Path(args.out) / "gguf"
        model.save_pretrained_gguf(str(gguf), tok, quantization_method=args.gguf)
        print(f"GGUF saved to {gguf} — see news/lora/README.md for the Ollama Modelfile")


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
