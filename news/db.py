"""SQLite layer for news commentary."""
import os
import sqlite3
from pathlib import Path

# news.db is tracked in git — GitHub Actions commits updates back to the repo
# each run, so state persists across cron invocations without a volume.
# NEWS_DB env can override (useful for tests).
DB_PATH = Path(os.environ.get("NEWS_DB", Path(__file__).parent / "news.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT UNIQUE NOT NULL,
  source        TEXT,
  source_title  TEXT,
  raw_text      TEXT,
  fetched_at    TEXT NOT NULL,

  title         TEXT,
  axiom         TEXT,
  body          TEXT,
  title_en      TEXT,
  axiom_en      TEXT,
  body_en       TEXT,
  tag           TEXT,
  violence_layer TEXT,
  your_edits    TEXT,
  published     INTEGER NOT NULL DEFAULT 0,

  model         TEXT,
  input_tokens  INTEGER,
  cache_read_tokens INTEGER,
  cache_create_tokens INTEGER,
  output_tokens INTEGER,
  generated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at
  ON articles(published, generated_at DESC);

-- URLs that failed (paywall, 403, extraction, LLM error). After
-- MAX_URL_FAILURES tries the crawler and pipeline stop picking them up, so
-- they no longer take the per-run slots.
CREATE TABLE IF NOT EXISTS failed_urls (
  url        TEXT PRIMARY KEY,
  tries      INTEGER NOT NULL DEFAULT 1,
  last_error TEXT,
  last_at    TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


MAX_URL_FAILURES = 2


def record_failure(url: str, error: str) -> None:
    from datetime import datetime, timezone
    with connect() as conn:
        conn.execute(
            """INSERT INTO failed_urls (url, tries, last_error, last_at) VALUES (?, 1, ?, ?)
               ON CONFLICT(url) DO UPDATE SET tries = tries + 1,
                 last_error = excluded.last_error, last_at = excluded.last_at""",
            (url, error[:300], datetime.now(timezone.utc).isoformat()),
        )


def given_up_urls() -> set[str]:
    with connect() as conn:
        return {r["url"] for r in conn.execute(
            "SELECT url FROM failed_urls WHERE tries >= ?", (MAX_URL_FAILURES,))}

if __name__ == "__main__":
    with connect() as c:
        print("schema ready at", DB_PATH)
        print("tables:", [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")])
