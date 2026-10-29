#!/usr/bin/env python3
"""One-shot pipeline — Bluesky crawl → generate commentary → render HTML → git push.

Steps:
  1. Crawl Bluesky using accounts.txt, append new URLs to urls.txt
  2. For each URL in urls.txt not yet in news.db, fetch + generate commentary
  3. Re-render news/index.html
  4. Commit + push news/index.html to GitHub (so Vercel rebuilds)

Run by GitHub Actions (.github/workflows/cron.yml) every 10 minutes. The
workflow sets SKIP_PUSH=1 and does the commit + push itself.

Env vars:
  PROVIDER           anthropic | ollama | openai (default ollama)
  OLLAMA_API_KEY, OLLAMA_MODEL         (PROVIDER=ollama)
  ANTHROPIC_API_KEY, ANTHROPIC_MODEL   (PROVIDER=anthropic)
  OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL  (PROVIDER=openai)
  GITHUB_TOKEN       (PAT with `repo` scope — for the final git push)
  GITHUB_REPO        (e.g. yangfei0770-boop/primalrace)
  GIT_USER_EMAIL     (e.g. bot@howtoraiseiq.com)
  GIT_USER_NAME      (e.g. breaking-news-bot)
  NEWS_DB            (e.g. /app/data/news.db)
  PUBLISH_NEW        (1 = auto-publish; 0 = drafts)
  SKIP_CRAWL         (1 = skip Bluesky step; 0 = crawl as normal)
  SKIP_PUSH          (1 = skip git push at end, useful locally)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from db import connect                                # noqa: E402
from generate import process_url, _check_env         # noqa: E402

NEWS_DIR = Path(__file__).parent
URLS_FILE = NEWS_DIR / "urls.txt"
RENDERED_HTML = NEWS_DIR / "index.html"
AUTO_PUBLISH = os.environ.get("PUBLISH_NEW", "0") == "1"
SKIP_CRAWL = os.environ.get("SKIP_CRAWL", "0") == "1"
SKIP_PUSH = os.environ.get("SKIP_PUSH", "0") == "1"
# Cap per cron run so we don't blow past the 30-min Actions timeout.
# Remaining URLs stay in urls.txt for the next run.
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "20"))
# Wall-clock cap — if Gemma is slow during peak, stop accepting new work after
# this much time has elapsed. Remainder waits for next hour.
SOFT_TIMEOUT_SEC = int(os.environ.get("SOFT_TIMEOUT_SEC", "1200"))  # 20 min


def crawl_bluesky() -> None:
    """Run the Bluesky crawler as a subprocess so any crash here doesn't kill
    the whole pipeline (we still want to process whatever's already in urls.txt)."""
    print("=== [1/3] Bluesky crawl ===")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "crawlers.bluesky"],
            cwd=Path(__file__).parent,
        )
    except subprocess.CalledProcessError as e:
        print(f"  [warn] crawl failed (exit {e.returncode}); continuing", file=sys.stderr)


def main():
    _check_env()

    if SKIP_CRAWL:
        print("[skip-crawl] SKIP_CRAWL=1")
    else:
        print("=== [1/4] Bluesky crawl ===")
        crawl_bluesky()

    print("\n=== [2/4] Generate commentary ===")

    if not URLS_FILE.exists():
        print("no urls.txt — nothing to generate")
    else:
        urls = [
            ln.strip() for ln in URLS_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        # Skip URLs already in the db so the MAX_PER_RUN cap counts only real work.
        with connect() as conn:
            seen = {r["url"] for r in conn.execute("SELECT url FROM articles")}
        pending = [u for u in urls if u not in seen]
        print(f"{len(urls)} URLs in queue, {len(pending)} new "
              f"(processing up to {MAX_PER_RUN} this run)")
        to_process = pending[:MAX_PER_RUN]

        new_ids = []
        started = time.time()
        for url in to_process:
            if time.time() - started > SOFT_TIMEOUT_SEC:
                print(f"  [soft-timeout] hit {SOFT_TIMEOUT_SEC}s "
                      f"— remaining URLs stay in queue for next run")
                break
            try:
                aid = process_url(url)
                if aid:  # 0 = already in db, muted or skipped: nothing to publish
                    new_ids.append(aid)
            except Exception as e:
                print(f"  [error] {url}: {e}", file=sys.stderr)

        if AUTO_PUBLISH and new_ids:
            with connect() as conn:
                conn.executemany(
                    "UPDATE articles SET published=1 WHERE id=?",
                    [(i,) for i in new_ids],
                )
            print(f"auto-published: {new_ids}")
            # bsky_post.py (separate workflow step, runs after git push)
            # picks these up and posts them to Bluesky
            (NEWS_DIR / ".new_ids").write_text(
                "\n".join(str(i) for i in new_ids), encoding="utf-8")

    print("\n=== [3/4] Render ===")
    subprocess.check_call([sys.executable, "render.py"], cwd=NEWS_DIR)

    print("\n=== [4/4] Push to GitHub ===")
    if SKIP_PUSH:
        print("[skip-push] SKIP_PUSH=1")
    else:
        push_to_github()

    print("\ndone.")


# ============================================================================
# Git push so Vercel re-deploys the static HTML
# ============================================================================

def push_to_github() -> None:
    """Commit news/index.html (and optionally news.db) and push to the repo.

    Inside a git checkout (local dev) it commits in place; otherwise it
    works in an ephemeral clone. Unused under GitHub Actions (SKIP_PUSH=1).
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo  = os.environ.get("GITHUB_REPO", "").strip()
    email = os.environ.get("GIT_USER_EMAIL", "bot@howtoraiseiq.com").strip()
    name  = os.environ.get("GIT_USER_NAME", "breaking-news-bot").strip()
    branch = os.environ.get("GIT_BRANCH", "main").strip()

    if not token or not repo:
        print("[warn] GITHUB_TOKEN or GITHUB_REPO not set — skipping push", file=sys.stderr)
        return

    # If the current working tree IS a git checkout of the right repo (local dev),
    # just commit+push from here. Otherwise (no checkout), clone fresh.
    blog_root = NEWS_DIR.parent
    if (blog_root / ".git").exists():
        _git_commit_push_in_place(blog_root, token, repo, email, name, branch)
    else:
        _git_clone_apply_push(token, repo, email, name, branch)


def _run(cmd: list, cwd: Path, env_extra: Optional[dict] = None) -> None:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    print(f"  $ {' '.join(cmd)}  (cwd={cwd})")
    subprocess.check_call(cmd, cwd=str(cwd), env=env)


def _git_commit_push_in_place(repo_root: Path, token: str, repo: str,
                              email: str, name: str, branch: str) -> None:
    _run(["git", "config", "user.email", email], repo_root)
    _run(["git", "config", "user.name", name], repo_root)
    _run(["git", "add", "news/index.html"], repo_root)
    # commit may fail if nothing changed — tolerate that
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_root),
    )
    if result.returncode == 0:
        print("  [skip] no changes to commit")
        return
    _run(["git", "commit", "-m", "auto: refresh news commentary"], repo_root)
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run(["git", "push", remote_url, f"HEAD:{branch}"], repo_root)


def _git_clone_apply_push(token: str, repo: str, email: str, name: str,
                          branch: str) -> None:
    """Clone fresh, copy the new index.html in, commit, push."""
    workdir = Path("/tmp/breaking-news-push")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run(["git", "clone", "--depth", "1", "--branch", branch,
          remote_url, str(workdir)], workdir.parent)

    # copy our freshly rendered HTML over
    target = workdir / "news" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RENDERED_HTML, target)

    _run(["git", "config", "user.email", email], workdir)
    _run(["git", "config", "user.name", name], workdir)
    _run(["git", "add", "news/index.html"], workdir)

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(workdir),
    )
    if result.returncode == 0:
        print("  [skip] no changes to commit")
        return

    _run(["git", "commit", "-m", "auto: refresh news commentary"], workdir)
    _run(["git", "push", remote_url, f"HEAD:{branch}"], workdir)


if __name__ == "__main__":
    main()
