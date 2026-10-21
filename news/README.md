# breaking news

新闻评论站，自动从 Bluesky 抓主流媒体新闻，用 Claude Fable 5.1（Anthropic API）按
《原初种族》(The Primal Race) 的哲学框架与费扬本人的写作风格生成中英双语评论。

部署：GitHub Actions 每 10 分钟跑一次流水线并 commit 回仓库 → Vercel 自动部署到 howtoraiseiq.com/news

## 文件结构

```
news/
├── serve.py              # Local preview server
├── requirements.txt
├── .env                  # API keys (gitignored)
├── .env.example
│
├── db.py                 # SQLite layer (NEWS_DB env can override path)
├── llm.py                # provider abstraction (anthropic | ollama | openai)
├── prompts/system.py     # 4-layer system prompt + book corpus
│
├── crawlers/bluesky.py   # Pull from accounts.txt → urls.txt
├── generate.py           # URL → fetch → LLM → row in db
├── render.py             # db → index.html (bilingual toggle)
├── admin.py              # list / show / publish / unpublish / edit / delete
├── admin_server.py       # local web admin (localhost:8080)
├── backfill_translation.py  # zh-only rows → fill English
├── compare_models.py     # Gemma vs Gemini output comparison
│
├── cron_pipeline.py      # crawl → generate → render (GitHub Actions, every 10 min)
├── social_post.py        # post new articles to Bluesky + X
├── bsky_post.py          # Bluesky posting helpers
├── bsky_delete_all.py    # delete all Bluesky posts (manual workflow)
│
├── accounts.txt          # Bluesky news handles
├── urls.txt              # Crawled URLs queue
├── news.db               # SQLite db (tracked in git)
├── index.html, p/, feed.xml  # rendered output
```

## 部署

详见 [DEPLOY.md](DEPLOY.md)。
