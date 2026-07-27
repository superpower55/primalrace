#!/usr/bin/env python3
"""Render published commentary from news.db to static HTML.

Outputs:
  news/index.html      — the main feed page (all articles)
  news/p/<id>.html     — one standalone page per article (SEO permalinks)
  news/feed.xml        — RSS 2.0 feed (latest 100)
  sitemap.xml          — repo-root sitemap covering /, /news/ and all articles

Only published=1 rows are rendered (use admin.py to publish).

Usage:
  python3 render.py            # only published items
  python3 render.py --drafts   # include drafts on index.html (local preview);
                               # standalone pages / feed / sitemap stay published-only
"""
import argparse
import html
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from db import connect

NEWS_DIR = Path(__file__).parent
OUT = NEWS_DIR / "index.html"
P_DIR = NEWS_DIR / "p"
FEED_OUT = NEWS_DIR / "feed.xml"
SITEMAP_OUT = NEWS_DIR.parent / "sitemap.xml"

SITE = "https://www.howtoraiseiq.com"
OG_IMAGE = f"{SITE}/og.png"

TAG_LABELS = {
    "china_tech":    "中国科技",
    "gender":        "性别",
    "international": "国际",
    "philosophy":    "哲学",
    "tech":          "科技",
    "good_news":     "好消息",
    "other":         "其他",
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{head_title}</title>
<meta name="description" content="{meta_desc}">
<meta name="author" content="费扬 Yang Fei">
<link rel="canonical" href="{canonical}">
<link rel="alternate" type="application/rss+xml" title="breaking news · 费扬" href="{site}/news/feed.xml">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{head_title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="The Primal Race">
<meta property="og:image" content="{og_image}">
<meta name="twitter:card" content="summary_large_image">
{extra_head}<script>
window.dataLayer = window.dataLayer || [];
function gtag(){{dataLayer.push(arguments);}}
// owner opt-out: visit once with ?me=1 to stop being counted on this device
try {{
  var q = new URLSearchParams(location.search);
  if (q.get('me') === '1') localStorage.setItem('pr-owner', '1');
  if (q.get('me') === '0') localStorage.removeItem('pr-owner');
}} catch (e) {{}}
// skip analytics for headless crawlers + the site owner
if (!navigator.webdriver && localStorage.getItem('pr-owner') !== '1') {{
  ['/_vercel/insights/script.js', '/_vercel/speed-insights/script.js',
   'https://www.googletagmanager.com/gtag/js?id=G-KRTMG8HV7F'].forEach(function (src) {{
    var s = document.createElement('script');
    s.defer = true; s.src = src;
    document.head.appendChild(s);
  }});
  gtag('js', new Date());
  gtag('config', 'G-KRTMG8HV7F');
}}
</script>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --ink: #1a1109;
  --paper: #f5f0e8;
  --accent: #8b1a1a;
  --accent-light: #b52020;
  --muted: #6b5c47;
  --border: #c9bfad;
}}
html, body {{ min-height: 100%; }}
body {{
  background: var(--paper);
  color: var(--ink);
  font-family: "Georgia", "Noto Serif SC", "Source Han Serif CN", serif;
  padding: 2.5rem 1.2rem 4rem;
  line-height: 1.75;
}}
body::before {{
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background: repeating-linear-gradient(0deg,
    transparent, transparent 28px,
    rgba(0,0,0,0.025) 28px, rgba(0,0,0,0.025) 29px);
}}
.wrap {{ max-width: 720px; margin: 0 auto; position: relative; z-index: 1; }}

header.masthead {{
  text-align: center;
  border-bottom: 1px solid var(--border);
  border-top: 5px solid var(--accent);
  padding: 2.2rem 0 1.4rem;
  margin-bottom: 2.4rem;
  background: rgba(255,253,248,0.6);
}}
.masthead .ornament {{
  color: var(--accent); font-size: 1.1rem;
  letter-spacing: 0.4em; display: block; margin-bottom: 0.8rem;
}}
.masthead h1 {{
  font-size: 2.4rem; font-weight: 700;
  letter-spacing: 0.12em; line-height: 1.1;
}}
.masthead h1 a {{ color: inherit; text-decoration: none; }}
.masthead .subtitle {{
  font-size: 0.85rem; letter-spacing: 0.22em;
  text-transform: uppercase; color: var(--muted);
  margin-top: 0.4rem;
}}
.masthead .byline {{
  font-size: 0.82rem; color: var(--muted);
  margin-top: 1rem; font-style: italic;
}}
.masthead .byline a {{ color: var(--accent); text-decoration: none; }}
.masthead .byline a:hover {{ text-decoration: underline; }}

.crumbs {{
  margin-bottom: 1.6rem; font-size: 0.85rem;
}}
.crumbs a {{ color: var(--accent); text-decoration: none; }}
.crumbs a:hover {{ text-decoration: underline; }}

article {{
  background: rgba(255,253,248,0.92);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  padding: 2rem 2.2rem 1.8rem;
  margin-bottom: 2rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 4px 16px rgba(0,0,0,0.04);
}}
article.draft {{ border-left-color: var(--muted); opacity: 0.78; }}
article h2 {{
  font-size: 1.55rem; line-height: 1.35;
  margin-bottom: 0.55rem; color: var(--ink);
  letter-spacing: 0.02em;
}}
article h2 a {{ color: inherit; text-decoration: none; }}
article h2 a:hover {{ color: var(--accent); }}
.meta {{
  font-size: 0.78rem; color: var(--muted);
  letter-spacing: 0.05em; margin-bottom: 1.2rem;
  display: flex; flex-wrap: wrap; gap: 0.8rem;
  border-bottom: 1px dashed var(--border);
  padding-bottom: 0.8rem;
}}
.meta .tag {{
  background: var(--accent); color: #fff;
  padding: 0.08rem 0.55rem; letter-spacing: 0.08em;
  font-size: 0.72rem;
}}
.meta .tag.good_news {{ background: #2d6a3e; }}
article.good_news {{ border-left-color: #2d6a3e; }}
article.good_news .axiom {{ color: #2d6a3e; border-left-color: #2d6a3e; }}
.meta .layer {{
  color: var(--accent); font-style: italic;
}}
.meta .source a {{ color: var(--muted); text-decoration: none; }}
.meta .source a:hover {{ color: var(--accent); text-decoration: underline; }}
.meta .permalink a {{ color: var(--muted); text-decoration: none; }}
.meta .permalink a:hover {{ color: var(--accent); }}

.axiom {{
  font-size: 1.05rem; font-style: italic;
  color: var(--accent);
  border-left: 2px solid var(--accent);
  padding-left: 0.9rem; margin-bottom: 1.2rem;
  line-height: 1.55;
}}
.body p {{
  margin-bottom: 1rem; text-align: justify;
  text-indent: 2em;
  font-size: 1.02rem;
}}
.body p:last-child {{ margin-bottom: 0; }}
.body.lang-en p {{ text-indent: 0; }}

/* language toggle */
.lang-switch {{
  position: fixed; top: 1rem; right: 1.2rem; z-index: 10;
  background: rgba(255,253,248,0.95);
  border: 1px solid var(--border);
  font-family: inherit; font-size: 0.8rem;
  letter-spacing: 0.12em;
}}
.lang-switch button {{
  background: transparent; border: none; cursor: pointer;
  padding: 0.45rem 0.85rem; font: inherit; color: var(--muted);
  border-right: 1px solid var(--border);
}}
.lang-switch button:last-child {{ border-right: none; }}
.lang-switch button.active {{ background: var(--accent); color: #fff; }}
body[data-lang="zh"] .lang-en {{ display: none; }}
body[data-lang="en"] .lang-zh {{ display: none; }}
body[data-lang="en"] .meta .layer {{ font-style: italic; }}
.no-translation {{
  font-size: 0.85rem; color: var(--muted);
  font-style: italic; padding: 0.6rem 0;
}}

.draft-badge {{
  display: inline-block; background: var(--muted); color: #fff;
  font-size: 0.7rem; letter-spacing: 0.1em;
  padding: 0.1rem 0.5rem; margin-left: 0.6rem;
  vertical-align: middle;
}}

footer.foot {{
  text-align: center; margin-top: 3.5rem;
  font-size: 0.78rem; color: var(--muted);
  letter-spacing: 0.18em;
}}
footer.foot a {{ color: var(--accent); text-decoration: none; }}

@media (max-width: 560px) {{
  body {{ padding: 1.6rem 0.9rem 3rem; }}
  .masthead h1 {{ font-size: 1.8rem; }}
  article {{ padding: 1.4rem 1.2rem; }}
  article h2 {{ font-size: 1.3rem; }}
}}
</style>
</head>
<body data-lang="zh">

<div class="lang-switch">
  <button data-set="zh" class="active">中文</button>
  <button data-set="en">EN</button>
</div>

<div class="wrap">

<header class="masthead">
  <span class="ornament">✦ &nbsp; ✦ &nbsp; ✦</span>
  <h1><a href="/news/">breaking news</a></h1>
  <div class="subtitle">News, read through The Primal Race</div>
  <div class="byline">
    <span class="lang-zh">by 费扬 &nbsp;·&nbsp; 数据 Claude &nbsp;·&nbsp;
    <a href="/">回到《原初种族》</a> &nbsp;·&nbsp;
    <a href="/news/feed.xml">RSS</a></span>
    <span class="lang-en">by Yang Fei &nbsp;·&nbsp; data Claude &nbsp;·&nbsp;
    <a href="/">back to The Primal Race</a> &nbsp;·&nbsp;
    <a href="/news/feed.xml">RSS</a></span>
  </div>
</header>

{crumbs}{articles}

<footer class="foot">
  ― &nbsp; ― &nbsp; ―<br>
  {footer_line}
</footer>

</div>

<script>
(function() {{
  var key = 'breaking-news-lang';
  var saved = localStorage.getItem(key) || 'zh';
  setLang(saved);
  document.querySelectorAll('.lang-switch button').forEach(function(b) {{
    b.addEventListener('click', function() {{
      setLang(b.dataset.set);
      localStorage.setItem(key, b.dataset.set);
    }});
  }});
  function setLang(l) {{
    document.body.dataset.lang = l;
    document.querySelectorAll('.lang-switch button').forEach(function(b) {{
      b.classList.toggle('active', b.dataset.set === l);
    }});
  }}
}})();
</script>
</body>
</html>
"""

ARTICLE_TEMPLATE = """<article class="{article_cls}" id="a{id}">
  <h2><a href="{permalink}"><span class="lang-zh">{title}</span><span class="lang-en">{title_en}</span></a>{draft_badge}</h2>
  <div class="meta">
    <span class="tag {tag_class}">{tag}</span>
    <span class="layer">{layer}</span>
    <span class="source">{source_link}</span>
    <span class="date">{date}</span>
    <span class="permalink"><a href="{permalink}">§ 链接</a></span>
  </div>
  {axiom_block_zh}
  {axiom_block_en}
  <div class="body lang-zh">{body}</div>
  {body_en_block}
</article>"""


def render_body(text: str) -> str:
    """Markdown-ish paragraph splitting + escaping."""
    paras = re.split(r"\n\s*\n", text.strip())
    return "\n  ".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>"
                       for p in paras if p.strip())


def render_layer(raw: str) -> str:
    if not raw:
        return ""
    labels = {
        "direct": "直接层",
        "structural": "结构层",
        "cultural": "文化层",
        "meta": "元暴力",
    }
    parts = [labels.get(p.strip(), p.strip()) for p in raw.split(",") if p.strip()]
    return " · ".join(parts)


def render_source(url: str, source: str) -> str:
    s = html.escape(source or "未知")
    if url.startswith("http"):
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{s} ↗</a>'
    return s


def parse_dt(raw: str) -> datetime:
    """Best-effort parse of generated_at; fall back to now (UTC)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime((raw or "")[:len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def attr(s: str, limit: int = 300) -> str:
    """Escape + squash whitespace for use inside an HTML attribute."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return html.escape(s, quote=True)


def article_html(r, include_drafts: bool) -> str:
    is_draft = not r["published"]
    date = (r["generated_at"] or "")[:10]
    axiom = (r["axiom"] or "").strip()
    axiom_en = (r["axiom_en"] or "").strip()
    body_text = (r["your_edits"] or r["body"] or "").strip()
    body_en = (r["body_en"] or "").strip()
    title_en = (r["title_en"] or "").strip()

    if body_en:
        body_en_html = f'<div class="body lang-en">{render_body(body_en)}</div>'
    else:
        body_en_html = ('<div class="body lang-en"><p class="no-translation">'
                        '(English translation pending — run backfill_translation.py)</p></div>')
    if not title_en:
        title_en = html.escape(r["title"] or "(untitled)")

    tag_raw = (r["tag"] or "other").strip()
    article_classes = []
    if is_draft:
        article_classes.append("draft")
    if tag_raw == "good_news":
        article_classes.append("good_news")
    return ARTICLE_TEMPLATE.format(
        id=r["id"],
        permalink=f"/news/p/{r['id']}",
        article_cls=" ".join(article_classes),
        draft_badge='<span class="draft-badge">DRAFT</span>' if is_draft else "",
        title=html.escape(r["title"] or "(untitled)"),
        title_en=html.escape(title_en),
        tag=TAG_LABELS.get(tag_raw, tag_raw or "—"),
        tag_class=tag_raw,
        layer=render_layer(r["violence_layer"] or ""),
        source_link=render_source(r["url"] or "", r["source"] or ""),
        date=date,
        axiom_block_zh=f'<div class="axiom lang-zh">{html.escape(axiom)}</div>' if axiom else "",
        axiom_block_en=f'<div class="axiom lang-en">{html.escape(axiom_en)}</div>' if axiom_en else "",
        body=render_body(body_text) if body_text else "<p><em>(no body)</em></p>",
        body_en_block=body_en_html,
    )


def write_article_pages(published_rows) -> int:
    """One standalone page per published article → news/p/<id>.html"""
    P_DIR.mkdir(exist_ok=True)
    n = 0
    for r in published_rows:
        title = (r["title"] or "(untitled)").strip()
        axiom = (r["axiom"] or "").strip()
        body_text = (r["your_edits"] or r["body"] or "").strip()
        desc_src = axiom or body_text[:160]
        url = f"{SITE}/news/p/{r['id']}"
        dt = parse_dt(r["generated_at"] or "")

        ld = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": title,
            "description": re.sub(r"\s+", " ", desc_src)[:200],
            "datePublished": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "inLanguage": "zh",
            "author": {"@type": "Person", "name": "Yang Fei", "alternateName": "费扬"},
            "mainEntityOfPage": url,
            "isPartOf": {"@type": "WebSite", "name": "The Primal Race", "url": SITE},
        }
        extra_head = ('<script type="application/ld+json">'
                      + json.dumps(ld, ensure_ascii=True)
                      + "</script>\n")

        page = PAGE_TEMPLATE.format(
            head_title=attr(f"{title} — breaking news · 费扬", 90),
            meta_desc=attr(f"{desc_src} — 用《原初种族》的框架读新闻，费扬评论。", 300),
            og_desc=attr(desc_src, 200),
            canonical=url,
            og_type="article",
            og_image=OG_IMAGE,
            site=SITE,
            extra_head=extra_head,
            crumbs='<div class="crumbs"><a href="/news/">← 全部评论 · all commentary</a></div>\n',
            articles=article_html(r, include_drafts=False),
            footer_line=f'<a href="/news/">breaking news</a> &nbsp;·&nbsp; <a href="/">原初种族 The Primal Race</a>',
        )
        (P_DIR / f"{r['id']}.html").write_text(page, encoding="utf-8")
        n += 1
    return n


def write_feed(published_rows) -> None:
    """RSS 2.0 — latest 100 published items."""
    items = []
    for r in published_rows[:100]:
        title = (r["title"] or "(untitled)").strip()
        axiom = (r["axiom"] or "").strip()
        body_text = (r["your_edits"] or r["body"] or "").strip()
        desc = axiom + ("\n\n" + body_text if body_text else "")
        url = f"{SITE}/news/p/{r['id']}"
        dt = parse_dt(r["generated_at"] or "")
        items.append(
            "<item>"
            f"<title>{html.escape(title)}</title>"
            f"<link>{url}</link>"
            f"<guid isPermaLink=\"true\">{url}</guid>"
            f"<pubDate>{format_datetime(dt)}</pubDate>"
            f"<description>{html.escape(desc[:1500])}</description>"
            "</item>"
        )
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>\n"
        "<title>breaking news · 费扬</title>\n"
        f"<link>{SITE}/news/</link>\n"
        f'<atom:link href="{SITE}/news/feed.xml" rel="self" type="application/rss+xml"/>\n'
        "<description>用《原初种族》的框架读新闻 — 费扬的双语新闻评论，每 10 分钟自动更新。</description>\n"
        "<language>zh</language>\n"
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )
    FEED_OUT.write_text(feed, encoding="utf-8")


def write_sitemap(published_rows) -> None:
    urls = [
        (f"{SITE}/", "weekly", "1.0", None),
        (f"{SITE}/news/", "hourly", "0.9", None),
        (f"{SITE}/novel", "weekly", "0.8", None),
        (f"{SITE}/poetry", "monthly", "0.7", None),
        (f"{SITE}/posts", "daily", "0.6", None),
    ]
    for r in published_rows:
        lastmod = (r["generated_at"] or "")[:10] or None
        urls.append((f"{SITE}/news/p/{r['id']}", None, "0.6", lastmod))

    entries = []
    for loc, freq, prio, lastmod in urls:
        e = f"<url><loc>{loc}</loc>"
        if lastmod:
            e += f"<lastmod>{lastmod}</lastmod>"
        if freq:
            e += f"<changefreq>{freq}</changefreq>"
        if prio:
            e += f"<priority>{prio}</priority>"
        e += "</url>"
        entries.append(e)

    SITEMAP_OUT.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n",
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", action="store_true", help="include unpublished drafts")
    args = ap.parse_args()

    where = "" if args.drafts else "WHERE published = 1"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM articles {where} "
            f"ORDER BY published DESC, generated_at DESC"
        ).fetchall()

    published_rows = [r for r in rows if r["published"]]

    # 1. index page
    parts = [article_html(r, args.drafts) for r in rows]
    html_out = PAGE_TEMPLATE.format(
        head_title="breaking news · 费扬 — 用《原初种族》的框架读新闻",
        meta_desc=attr("费扬的双语新闻评论：用《原初种族》的分析框架（暴力三角、共谋者理论、元暴力）读全球新闻。中英双语，每 10 分钟自动更新。Bilingual news commentary through The Primal Race framework, auto-refreshed every 10 minutes."),
        og_desc=attr("暴力三角、共谋者理论、元暴力——不写'客观中立'，直接给判断。中英双语，每 10 分钟更新。"),
        canonical=f"{SITE}/news/",
        og_type="website",
        og_image=OG_IMAGE,
        site=SITE,
        extra_head="",
        crumbs="",
        articles="\n".join(parts) if parts
                else '<article><div class="body"><p>暂无评论。</p></div></article>',
        footer_line=f"共 {len(rows)} 条 &nbsp;·&nbsp; 更新于 "
                    + datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    OUT.write_text(html_out, encoding="utf-8")
    print(f"wrote {OUT}  ({len(rows)} articles, drafts_included={args.drafts})")

    # 2. per-article pages
    n = write_article_pages(published_rows)
    print(f"wrote {n} article pages → {P_DIR}/")

    # 3. RSS feed
    write_feed(published_rows)
    print(f"wrote {FEED_OUT}")

    # 4. sitemap
    write_sitemap(published_rows)
    print(f"wrote {SITEMAP_OUT}")


if __name__ == "__main__":
    main()
