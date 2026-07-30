// Serverless translation endpoint (Vercel Function).
// Uses Gemma via Ollama Cloud for quality; falls back to Google MT on
// failure/timeout so the caller always gets something back.
//
// Env vars (set in Vercel → Settings → Environment Variables):
//   OLLAMA_API_KEY   (required for Gemma; without it we use Google only)
//   OLLAMA_BASE_URL  (default https://ollama.com)
//   OLLAMA_MODEL     (default gemma4:31b-cloud)

async function gemmaTranslate(text, langName) {
  const key = (process.env.OLLAMA_API_KEY || '').trim();
  if (!key) throw new Error('no OLLAMA_API_KEY');
  const base = (process.env.OLLAMA_BASE_URL || 'https://ollama.com').replace(/\/+$/, '');
  const model = process.env.OLLAMA_MODEL || 'gemma4:31b-cloud';

  const sys = 'You are a professional literary translator. Translate the '
    + 'user\'s text into ' + langName + '. Preserve tone, voice, rhythm and '
    + 'meaning; render idioms and slang naturally rather than word-for-word. '
    + 'Output ONLY the translation — no preamble, no notes, no quotation marks.';

  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 9000);
  try {
    const r = await fetch(base + '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: sys },
          { role: 'user', content: text },
        ],
        stream: false,
        options: { temperature: 0.3 },
      }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error('ollama ' + r.status);
    const data = await r.json();
    const out = (data && data.message && data.message.content || '').trim();
    if (!out) throw new Error('empty');
    return out;
  } finally {
    clearTimeout(t);
  }
}

async function googleTranslate(text, tl) {
  const u = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl='
    + tl + '&dt=t&q=' + encodeURIComponent(text);
  const r = await fetch(u);
  const data = await r.json();
  let out = '';
  (data[0] || []).forEach((seg) => { if (seg && seg[0]) out += seg[0]; });
  return out.trim();
}

export default async function handler(req, res) {
  let text = '';
  try {
    if (req.method === 'POST') {
      const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});
      text = body.text || '';
    } else {
      text = (req.query && req.query.text) || '';
    }
  } catch (e) { text = ''; }

  text = String(text).slice(0, 4000).trim();
  if (!text) { res.status(400).json({ error: 'no text' }); return; }

  const hasCJK = /[一-鿿㐀-䶿]/.test(text);
  const langName = hasCJK ? 'English' : 'Chinese';
  const googleTl = hasCJK ? 'en' : 'zh-CN';

  try {
    const translation = await gemmaTranslate(text, langName);
    res.setHeader('Cache-Control', 's-maxage=86400');
    res.status(200).json({ translation, engine: 'gemma' });
  } catch (e) {
    try {
      const translation = await googleTranslate(text, googleTl);
      res.setHeader('Cache-Control', 's-maxage=86400');
      res.status(200).json({ translation, engine: 'google' });
    } catch (e2) {
      res.status(502).json({ error: 'translation failed' });
    }
  }
}
