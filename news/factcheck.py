"""Check a generated commentary against its source article.

A second model reads the article and the commentary (zh + en) and lists every
specific factual claim the article does not support. Opinions, framework
readings and wording are out of scope — only checkable facts about the story.

  FACTCHECK_MODEL   default claude-sonnet-5-5
  ANTHROPIC_WORKSPACE_ID  needed only for keys not scoped to a workspace
"""
from __future__ import annotations

import json
import os

# USD per million tokens for the checker model (Claude Sonnet 5.5).
PRICE_INPUT = 2.00
PRICE_OUTPUT = 10.00

SYSTEM = """你是新闻评论的事实核查员。你会拿到一篇新闻报道原文，以及一篇根据它写的评论（中文版和英文版）。

你的任务：找出评论里关于这条新闻的人和事的具体陈述——数字、日期、时长、刑期、金额、引语、
头衔、身份、先后顺序、谁做了什么、谁说了什么——其中报道原文不支持的，或者和原文矛盾的。

不要报告的：
- 观点、判断、比喻、修辞、框架解读（"这是共谋""这是元暴力"之类），哪怕你不同意
- 原文里有、只是换了说法或单位的（"a million and a half" = "一百五十万"）
- 中英文版本之间的措辞差异，除非其中一版写错了事实
- 不涉及具体细节的常识性背景

每条问题写清楚评论里的原话、原文实际怎么说（或原文没提）。severity 填：
- contradicts：和原文矛盾
- not_in_article：原文没有这个信息，是评论自己补的
没有问题就返回空列表。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "problem": {"type": "string"},
                    "severity": {"type": "string", "enum": ["contradicts", "not_in_article"]},
                },
                "required": ["claim", "problem", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["issues"],
    "additionalProperties": False,
}


def _client():
    import anthropic
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace} if workspace else None,
    )


def check(article: dict, commentary: dict) -> dict:
    """Returns {"issues": [...], "model", "cost_usd"}. Raises on refusal."""
    model = os.environ.get("FACTCHECK_MODEL") or "claude-sonnet-5-5"
    fields = ("title", "axiom", "body", "title_en", "axiom_en", "body_en")
    user_msg = (
        f"--- 报道原文 ---\n标题：{article['source_title']}\n来源：{article['source']}\n\n"
        f"{article['raw_text']}\n--- 原文结束 ---\n\n"
        f"--- 评论 ---\n"
        + "\n\n".join(f"[{f}]\n{commentary.get(f, '')}" for f in fields)
        + "\n--- 评论结束 ---"
    )
    resp = _client().beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"fact-check declined by {resp.model}")
    text = next(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return {
        "issues": json.loads(text)["issues"],
        "model": resp.model,
        "cost_usd": round((u.input_tokens * PRICE_INPUT + u.output_tokens * PRICE_OUTPUT) / 1e6, 4),
    }
