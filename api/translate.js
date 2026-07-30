// Serverless translation endpoint (Vercel Function).
// Tries Google Gemini (best quality) across a couple of free-tier models,
// then falls back to Google MT so the caller always gets something.
//
// Env vars (Vercel → Settings → Environment Variables):
//   GEMINI_API_KEY   from https://aistudio.google.com/apikey
//   GEMINI_MODEL     optional; overrides the model list below
//
// Debug: GET /api/translate?text=hello&debug=1  → shows key presence + errors.

function models() {
  const override = (process.env.GEMINI_MODEL || '').trim();
  if (override) return [override];
  return ['gemini-2.5-flash-lite', 'gemini-flash-latest', 'gemini-2.0-flash'];
}

async function geminiTry(text, langName, model, key) {
  const prompt = 'You are a professional literary translator. Translate the '
    + 'text below into ' + langName + '. Preserve tone, voice, rhythm and '
    + 'meaning; render idioms and slang naturally rather than word-for-word. '
    + 'Output ONLY the translation — no preamble, no notes, no quotation marks.\n\n'
    + text;
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
    + model + ':generateContent?key=' + encodeURIComponent(key);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 9000);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.3 },
      }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const body = await r.text();
      throw new Error(model + ' HTTP ' + r.status + ': ' + body.slice(0, 160));
    }
    const data = await r.json();
    const parts = (((data.candidates || [])[0] || {}).content || {}).parts;
    const txt = (parts && parts[0] && parts[0].text || '').trim();
    if (!txt) throw new Error(model + ' empty response');
    return txt;
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
  let debug = false;
  try {
    if (req.method === 'POST') {
      const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});
      text = body.text || '';
      debug = !!body.debug;
    } else {
      text = (req.query && req.query.text) || '';
      debug = !!(req.query && req.query.debug);
    }
  } catch (e) { text = ''; }

  text = String(text).slice(0, 4000).trim();
  if (!text) { res.status(400).json({ error: 'no text' }); return; }

  const hasCJK = /[一-鿿㐀-䶿]/.test(text);
  const langName = hasCJK ? 'English' : 'Chinese';
  const googleTl = hasCJK ? 'en' : 'zh-CN';

  const key = (process.env.GEMINI_API_KEY || '').trim();
  const errors = [];

  if (key) {
    for (const m of models()) {
      try {
        const translation = await geminiTry(text, langName, m, key);
        res.setHeader('Cache-Control', 's-maxage=86400');
        const out = { translation, engine: 'gemini:' + m };
        if (debug) out.errors = errors;
        res.status(200).json(out);
        return;
      } catch (e) {
        errors.push(String(e.message || e));
      }
    }
  } else {
    errors.push('GEMINI_API_KEY not set');
  }

  try {
    const translation = await googleTranslate(text, googleTl);
    res.setHeader('Cache-Control', 's-maxage=86400');
    const out = { translation, engine: 'google', hasKey: !!key };
    if (debug) out.errors = errors;
    res.status(200).json(out);
  } catch (e2) {
    res.status(502).json({ error: 'translation failed', hasKey: !!key, errors });
  }
}
