// Search by image (Vercel Function).
// Gemini describes the uploaded photo; the description is embedded and ranked
// against two indexes built by the news/ scripts:
//   - news/search          commentary text  -> "articles about this"
//   - news/search/images   article images, as Gemini descriptions written with
//                          the same prompt   -> "articles with a similar image"
//
// Env vars (Vercel → Settings → Environment Variables):
//   GEMINI_API_KEY        shared with api/translate.js and api/search.js
//   GEMINI_VISION_MODEL   optional, default gemini-2.5-flash-lite
//
// POST /api/image-search  {"image": "<base64 or data: URL>", "mime": "image/jpeg"}
//   → { description, articles: [...], images: [...] }

import { loadIndex, embedQuery, cosineScores } from './_lib/search-core.js';

// Keep in sync with DESCRIBE_PROMPT in news/build_image_index.py.
const DESCRIBE_PROMPT = '这是一张新闻配图。用中文写一段 60 到 120 字的客观描述：画面里有哪些人、物、场景、动作，'
  + '看得到的文字和标志，以及整体氛围。只写看得到的东西，不要猜测人物身份（除非图里的文字写明），'
  + '不要评论。最后另起一行，用英文列出 5 到 10 个关键词，用逗号分隔。';

const MAX_BASE64 = 3_000_000; // ~2.2 MB image; the page shrinks photos before upload

async function describe(data, mime, key) {
  const model = (process.env.GEMINI_VISION_MODEL || 'gemini-2.5-flash-lite').trim();
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
    + model + ':generateContent?key=' + encodeURIComponent(key);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [{ parts: [{ inline_data: { mime_type: mime, data } }, { text: DESCRIBE_PROMPT }] }],
        generationConfig: { temperature: 0.2, maxOutputTokens: 400 },
      }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error('describe HTTP ' + r.status + ': ' + (await r.text()).slice(0, 160));
    const parts = ((((await r.json()).candidates || [])[0] || {}).content || {}).parts || [];
    const text = parts.map((p) => p.text || '').join('').trim();
    if (!text) throw new Error('describe: empty response');
    return text;
  } finally {
    clearTimeout(t);
  }
}

function articleResult(m, score) {
  return {
    id: m.id, title: m.t, title_en: m.te, axiom: m.a, axiom_en: m.ae, date: m.d, tag: m.g,
    score: Math.round(score * 1000) / 1000, url: '/news/p/' + m.id,
  };
}

function imageResult(m, score) {
  return {
    id: m.id, image: m.img, caption: m.c, title: m.t, title_en: m.te, date: m.d,
    score: Math.round(score * 1000) / 1000, url: '/news/p/' + m.id,
  };
}

export default async function handler(req, res) {
  if (req.method !== 'POST') { res.status(405).json({ error: 'POST an image' }); return; }
  let body = req.body;
  try { if (typeof body === 'string') body = JSON.parse(body); } catch (e) { body = {}; }
  let data = String((body && body.image) || '');
  let mime = String((body && body.mime) || 'image/jpeg');
  const m = data.match(/^data:(image\/[\w.+-]+);base64,(.*)$/s);
  if (m) { mime = m[1]; data = m[2]; }
  if (!data || !/^image\//.test(mime)) { res.status(400).json({ error: 'no image' }); return; }
  if (data.length > MAX_BASE64) { res.status(413).json({ error: 'image too large' }); return; }

  const key = (process.env.GEMINI_API_KEY || '').trim();
  const textIdx = loadIndex('');
  const imageIdx = loadIndex('images');
  if (!key || !textIdx) { res.status(503).json({ error: 'search by image is not available' }); return; }

  let description, qv;
  try {
    description = await describe(data, mime, key);
    qv = await embedQuery(description, textIdx.info, key);
  } catch (e) {
    // Most often the free-tier quota for the day is spent.
    res.status(503).json({ error: 'search by image is busy, try again later' });
    return;
  }

  const top = (scores, n) => scores.sort((a, b) => b[0] - a[0]).slice(0, n);
  const articles = top(cosineScores(qv, textIdx), 10).map(([s, m]) => articleResult(m, s));
  const images = imageIdx && imageIdx.info.model === textIdx.info.model
    ? top(cosineScores(qv, imageIdx), 12).map(([s, m]) => imageResult(m, s))
    : [];
  res.status(200).json({ description, articles, images, imagesIndexed: imageIdx ? imageIdx.info.count : 0 });
}
