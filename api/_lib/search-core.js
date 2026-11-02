// Shared by api/search.js and api/image-search.js. The leading underscore
// keeps Vercel from serving this directory as an endpoint.

import { readFileSync } from 'fs';
import path from 'path';

const cache = {};

// Load news/search/<sub>/{info.json,meta.json,vectors.bin}; null if absent.
export function loadIndex(sub = '') {
  if (sub in cache) return cache[sub];
  const dir = path.join(process.cwd(), 'news', 'search', sub);
  try {
    const info = JSON.parse(readFileSync(path.join(dir, 'info.json'), 'utf8'));
    const meta = JSON.parse(readFileSync(path.join(dir, 'meta.json'), 'utf8'));
    const buf = readFileSync(path.join(dir, 'vectors.bin'));
    const vectors = new Int8Array(buf.buffer, buf.byteOffset, buf.byteLength);
    if (vectors.length !== meta.length * info.dims) throw new Error('index files disagree');
    cache[sub] = { info, meta, vectors };
  } catch (e) {
    cache[sub] = null;
  }
  return cache[sub];
}

// Embed a query with the index's Gemini model; returns a unit vector.
export async function embedQuery(text, info, key) {
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
        content: { parts: [{ text }] },
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

// Cosine scores (int8 rows are unit vectors scaled by 127) for every row.
export function cosineScores(qv, idx) {
  const { info, meta, vectors } = idx;
  const dims = info.dims;
  return meta.map((m, i) => {
    let dot = 0;
    const off = i * dims;
    for (let j = 0; j < dims; j++) dot += qv[j] * vectors[off + j];
    return [dot / 127, m];
  });
}
