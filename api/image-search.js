// Search by image (Vercel Function).
// The /news/ page embeds the searcher's photo in the browser with CLIP
// (transformers.js, Xenova/clip-vit-base-patch32, quantized) and posts the
// vector here. It is ranked against two indexes that news/build_image_index.py
// builds with the same model files:
//   news/search/clip/img.*   article lead images  -> "similar image"
//   news/search/clip/txt.*   original headlines   -> "articles about this"
// No API calls, no quota; the photo itself never leaves the browser.
//
// POST /api/image-search  {"vector": [512 numbers]}
//   → { images: [...], articles: [...] }

import { readFileSync } from 'fs';
import path from 'path';

const DIMS = 512;
let indexes = null;

function load(name) {
  const dir = path.join(process.cwd(), 'news', 'search', 'clip');
  const meta = JSON.parse(readFileSync(path.join(dir, name + '.json'), 'utf8'));
  const buf = readFileSync(path.join(dir, name + '.bin'));
  const vectors = new Int8Array(buf.buffer, buf.byteOffset, buf.byteLength);
  if (vectors.length !== meta.length * DIMS) throw new Error(name + ': index files disagree');
  return { meta, vectors };
}

function loadAll() {
  if (indexes === null) {
    try { indexes = { img: load('img'), txt: load('txt') }; } catch (e) { indexes = false; }
  }
  return indexes;
}

function top(q, idx, n) {
  const { meta, vectors } = idx;
  const scored = meta.map((m, i) => {
    let dot = 0;
    const off = i * DIMS;
    for (let j = 0; j < DIMS; j++) dot += q[j] * vectors[off + j];
    return [dot / 127, m];
  });
  return scored.sort((a, b) => b[0] - a[0]).slice(0, n);
}

const round = (s) => Math.round(s * 1000) / 1000;

export default async function handler(req, res) {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST a vector' }); return; }
  let body = req.body;
  try { if (typeof body === 'string') body = JSON.parse(body); } catch (e) { body = {}; }
  const v = body && body.vector;
  if (!Array.isArray(v) || v.length !== DIMS || !v.every((x) => typeof x === 'number' && isFinite(x))) {
    res.status(400).json({ error: 'expected {"vector": [' + DIMS + ' numbers]}' });
    return;
  }
  const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0)) || 1;
  const q = v.map((x) => x / norm);

  const idx = loadAll();
  if (!idx) { res.status(503).json({ error: 'image index not built yet' }); return; }

  const images = top(q, idx.img, 12).map(([s, m]) => ({
    id: m.id, image: m.img, title: m.t, title_en: m.te, date: m.d, score: round(s), url: '/news/p/' + m.id,
  }));
  const articles = top(q, idx.txt, 10).map(([s, m]) => ({
    id: m.id, title: m.t, title_en: m.te, axiom: m.a, axiom_en: m.ae, date: m.d, tag: m.g,
    score: round(s), url: '/news/p/' + m.id,
  }));
  res.status(200).json({ images, articles });
}
