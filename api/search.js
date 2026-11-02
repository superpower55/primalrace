// Semantic search over the news commentary (Vercel Function).
// The index is built by news/build_search_index.py: int8 vectors + metadata.
// A query is embedded with Gemini and ranked by cosine similarity; a title or
// axiom that contains the query verbatim gets a small boost. If Gemini is
// unavailable the endpoint falls back to plain keyword matching.
//
// Env vars (Vercel → Settings → Environment Variables):
//   GEMINI_API_KEY       shared with api/translate.js
//
// GET /api/search?q=代孕&n=20   → { mode, results: [{ id, title, ..., url }] }

import { readFileSync } from 'fs';
import path from 'path';

let index = null;

function loadIndex() {
  if (index) return index;
  const dir = path.join(process.cwd(), 'news', 'search');
  const info = JSON.parse(readFileSync(path.join(dir, 'info.json'), 'utf8'));
  const meta = JSON.parse(readFileSync(path.join(dir, 'meta.json'), 'utf8'));
  const buf = readFileSync(path.join(dir, 'vectors.bin'));
  const vectors = new Int8Array(buf.buffer, buf.byteOffset, buf.byteLength);
  if (vectors.length !== meta.length * info.dims) throw new Error('index files disagree');
  index = { info, meta, vectors };
  return index;
}

async function embedQuery(q, info, key) {
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
    + info.model + ':embedContent?key=' + encodeURIComponent(key);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 6000);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'models/' + info.model,
        content: { parts: [{ text: q }] },
        taskType: 'RETRIEVAL_QUERY',
        outputDimensionality: info.dims,
      }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error('embed HTTP ' + r.status + ': ' + (await r.text()).slice(0, 160));
    const values = ((await r.json()).embedding || {}).values || [];
    if (values.length !== info.dims) throw new Error('embed: bad dims ' + values.length);
    const norm = Math.sqrt(values.reduce((s, x) => s + x * x, 0)) || 1;
    return values.map((x) => x / norm);
  } finally {
    clearTimeout(t);
  }
}

function keywordHit(m, needle) {
  const hay = (m.t + '\n' + m.te + '\n' + m.a + '\n' + m.ae).toLowerCase();
  return hay.includes(needle);
}

// Query -> match units: whitespace-separated words, with CJK runs broken into
// overlapping two-character pieces ("南极海冰" -> 南极, 极海, 海冰), since
// Chinese has no spaces and an exact-phrase match is too strict.
function queryTerms(q) {
  const terms = [];
  q.toLowerCase().split(/\s+/).filter(Boolean).forEach((w) => {
    const cjk = w.match(/[\u3400-\u9fff]+/g) || [];
    const rest = w.replace(/[\u3400-\u9fff]+/g, ' ').split(/\s+/).filter((x) => x.length > 1);
    cjk.forEach((run) => {
      if (run.length <= 2) terms.push(run);
      else for (let i = 0; i < run.length - 1; i++) terms.push(run.slice(i, i + 2));
    });
    terms.push(...rest);
  });
  return [...new Set(terms)];
}

// Share of query terms found anywhere (title, axiom, headline, snippet),
// plus a bonus for the share found in the title/axiom/headline.
function keywordScore(m, terms) {
  if (!terms.length) return { cover: 0, score: 0 };
  const head = (m.t + '\n' + m.te + '\n' + m.a + '\n' + m.ae + '\n' + (m.o || '')).toLowerCase();
  const all = head + '\n' + m.s.toLowerCase();
  let found = 0, inHead = 0;
  terms.forEach((t) => { if (all.includes(t)) found += 1; if (head.includes(t)) inHead += 1; });
  const cover = found / terms.length;
  return { cover, score: cover + 0.5 * inHead / terms.length };
}

function toResult(m, score) {
  return {
    id: m.id, title: m.t, title_en: m.te, axiom: m.a, axiom_en: m.ae,
    date: m.d, tag: m.g, snippet: m.s, score: Math.round(score * 1000) / 1000,
    url: '/news/p/' + m.id,
  };
}

export default async function handler(req, res) {
  const q = String((req.query && req.query.q) || '').trim().slice(0, 200);
  const n = Math.min(50, Math.max(1, parseInt((req.query && req.query.n) || '20', 10) || 20));
  if (!q) { res.status(400).json({ error: 'no query' }); return; }

  let idx;
  try {
    idx = loadIndex();
  } catch (e) {
    res.status(503).json({ error: 'search index not built yet' });
    return;
  }
  const { info, meta, vectors } = idx;
  const needle = q.toLowerCase();
  const key = (process.env.GEMINI_API_KEY || '').trim();

  let qv = null;
  let error = null;
  if (key) {
    try { qv = await embedQuery(q, info, key); } catch (e) { error = String(e.message || e); }
  } else {
    error = 'GEMINI_API_KEY not set';
  }

  let scored;
  if (qv) {
    const dims = info.dims;
    scored = meta.map((m, i) => {
      let dot = 0;
      const off = i * dims;
      for (let j = 0; j < dims; j++) dot += qv[j] * vectors[off + j];
      const score = dot / 127 + (keywordHit(m, needle) ? 0.1 : 0);
      return [score, m];
    });
  } else {
    // Keyword fallback (no embedding available, e.g. quota spent): rank by
    // how many query terms appear; keep matches covering at least half.
    const terms = queryTerms(q);
    scored = [];
    meta.forEach((m) => {
      const { cover, score } = keywordScore(m, terms);
      if (cover >= 0.5) scored.push([score + (keywordHit(m, needle) ? 0.5 : 0), m]);
    });
  }
  scored.sort((a, b) => b[0] - a[0]);

  res.setHeader('Cache-Control', 's-maxage=3600, stale-while-revalidate=86400');
  const out = {
    mode: qv ? 'semantic' : 'keyword',
    count: info.count,
    results: scored.slice(0, n).map(([s, m]) => toResult(m, s)),
  };
  if (req.query && req.query.debug && error) out.error = error;
  res.status(200).json(out);
}
