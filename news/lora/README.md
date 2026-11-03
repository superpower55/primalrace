# Breaking-news LoRA: distilling DeepSeek into Gemma 4 12B

The site's commentary is written by DeepSeek (`deepseek-chat`), which reads
a ~36k-token system prompt: the framework, the style rules, and a book
excerpt. This folder fine-tunes Gemma 4 12B so that the voice and framework
live in the weights. The model then needs only a two-line system prompt and
can run on our own hardware.

## 1. Data: `export_lora_dataset.py` (no API calls)

Run it with `cd news && python3 export_lora_dataset.py`. It writes
`news/dataset/lora/train.jsonl`, `eval.jsonl` and `stats.json`.

Each example has three messages:
- **system:** the short `TRAIN_SYSTEM` (`prompts/system.py`);
- **user:** the article;
- **assistant:** the JSON DeepSeek wrote with the full prompt.

What goes in:
- Every published DeepSeek article that has a news summary.
- DeepSeek's skip decisions (weather reports, scores, quizzes), so the model
  also learns when not to comment.

What gets dropped:
- Missing fields.
- Odd body lengths.
- A summary number not in the article. 330万 ↔ $3.3 million counts as a match.

The split: about 10% goes to eval, fixed by article id, so the eval set never
leaks into training as the set grows.

Summaries for older articles are written by `backfill_summaries.py`, 20 per
pipeline run. Re-export after it has caught up.

## 2. Train: `train_lora.py` (rented GPU)

Do QLoRA, 4-bit, r=16, 2 epochs. Loss is only on the answer JSON. The prompt
is the chat template's own generation prompt, which ends in Gemma 4's empty
thinking block. That is exactly what Ollama or vLLM send at inference.

```bash
git clone https://github.com/superpower55/primalrace && cd primalrace
# copy news/dataset/lora/{train,eval}.jsonl over (scp), or export on the box
pip install -r news/lora/requirements.txt
python3 news/lora/train_lora.py --check-only     # token counts + mask, no GPU
python3 news/lora/train_lora.py --gguf q4_k_m    # adapter + GGUF for Ollama
```

Expected scale:
- **Examples:** ~1000 at a median of ~1.5k tokens.
- **Time:** about 1 h on an L40S or A100 (~$1–2/h on RunPod or Vast).
- **Budget:** $5–10 including setup.

A 24 GB card (4090, A10) also works with `--max-seq 6144`.

### Free option: Kaggle

`kaggle_train.ipynb` runs on Kaggle's free T4 GPU, which allows 30 h a week
and 12 h per run.

1. Upload `lora_dataset.zip` as a private Dataset.
2. Import the notebook.
3. Set the accelerator to GPU T4 x2, turn Internet on, and add the dataset as input.
4. Choose **Save & Run All**.

The run trains (about 4–6 h) and then generates 20 eval replies with
`generate_local.py`. It scores them with `eval_lora.py --replies`. The adapter
lands in the version's Output tab.

## 3. Serve with Ollama

```
# Modelfile
FROM ./news/lora/out/gguf/<the .gguf file>
PARAMETER temperature 0.3
PARAMETER num_ctx 8192
```

Then run `ollama create primal-gemma -f Modelfile`. A Q4_K_M 12B is about
7 GB. It runs on the Oracle free-tier A1, which has 24 GB RAM (CPU only:
slow, minutes per article).

## 4. Evaluate: `eval_lora.py`

```bash
python3 lora/eval_lora.py --reference                                   # the teacher, for scale
python3 lora/eval_lora.py --base-url http://localhost:11434/v1 --model gemma4:12b --tag base
python3 lora/eval_lora.py --base-url http://localhost:11434/v1 --model primal-gemma --tag lora
DEEPSEEK_API_KEY=... python3 lora/eval_lora.py ... --judge              # + DeepSeek 1–5 scores
```

It checks:
- JSON validity;
- skip agreement with the teacher;
- tag and violence-layer agreement;
- numbers not in the article;
- the 120-character summary cap;
- body length;
- use of the framework's terms.

Optionally, DeepSeek rates voice and faithfulness.

Baseline to beat: DeepSeek itself, given only the short prompt, commented on
3 of 3 sports reports that it skips with the full prompt. Its bodies were
~330 characters instead of ~1000, and it used few framework terms. Base Gemma
should look similar. The LoRA should close that gap.
