/**
 * Daily Rundown — Anthropic API proxy + article fetcher + rate limiter
 *
 * Two modes (selected by request body):
 *   { fetch_url: "https://..." }  →  fetches article server-side, returns { html, url }
 *   { model: "...", ... }         →  proxies request to Anthropic API (rate limited)
 *
 * Rate limit: WEEKLY_LIMIT card generations per week, tracked in Cloudflare KV.
 *
 * Deploy:
 *   1. wrangler kv namespace create RATE_LIMIT
 *      → copy the returned id into wrangler.toml [[kv_namespaces]] id field
 *   2. wrangler deploy
 *   3. wrangler secret put ANTHROPIC_API_KEY
 *   4. Add the *.workers.dev URL as WORKER_URL in GitHub repo secrets
 */

const WEEKLY_LIMIT = 10;

const ALLOWED_ORIGINS = [
  'https://dizchrisctrl.github.io',
  'http://localhost',
  'http://127.0.0.1',
];

// ── Newsletter helpers ────────────────────────────────────────────────────────

function isValidEmail(email) {
  return typeof email === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

/** Read sub_list from KV; returns array of { email, token } objects. */
async function readSubList(kv) {
  const raw = await kv.get('sub_list');
  return raw ? JSON.parse(raw) : [];
}

/** Write sub_list back to KV. */
async function writeSubList(kv, list) {
  await kv.put('sub_list', JSON.stringify(list));
}

/** Send an email via Resend. */
async function sendEmail(apiKey, { from, to, subject, html }) {
  const resp = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from, to, subject, html }),
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Resend error ${resp.status}: ${err}`);
  }
  return resp.json();
}

/** Simple HTML page response. */
function htmlPage(title, body) {
  return new Response(`<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<style>body{margin:0;background:#0f1117;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px}
.card{background:#1a1d2e;border-radius:16px;padding:40px 32px;max-width:440px;width:100%}
h1{font-size:1.5rem;font-weight:800;margin:0 0 12px}
p{color:#94a3b8;font-size:0.95rem;line-height:1.6;margin:0 0 20px}
a{color:#818cf8;text-decoration:none;font-weight:600}</style>
</head><body><div class="card">${body}</div></body></html>`,
    { status: 200, headers: { 'Content-Type': 'text/html;charset=UTF-8' } });
}

function corsHeaders(origin) {
  // Exact match only — startsWith() allows subdomain spoofing attacks
  const allowed = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin':  allowed,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age':       '86400',
  };
}

function jsonResponse(data, status, origin) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) },
  });
}

/** Returns "YYYY_WW" for the current ISO week (resets every Monday). */
function weekKey() {
  const now  = new Date();
  const day  = now.getUTCDay() || 7;                          // Mon=1 … Sun=7
  const monday = new Date(now);
  monday.setUTCDate(now.getUTCDate() - (day - 1));
  const y = monday.getUTCFullYear();
  const m = String(monday.getUTCMonth() + 1).padStart(2, '0');
  const d = String(monday.getUTCDate()).padStart(2, '0');
  return `cards_${y}_${m}_${d}`;                              // e.g. cards_2026_04_13
}

/** Returns { allowed, used, remaining } and increments the counter if allowed. */
async function checkAndIncrement(kv) {
  if (!kv) {
    // KV not bound — fail open so the app still works without KV configured
    return { allowed: true, used: 0, remaining: WEEKLY_LIMIT };
  }
  const key    = weekKey();
  const stored = await kv.get(key);
  const used   = parseInt(stored ?? '0', 10);

  if (used >= WEEKLY_LIMIT) {
    return { allowed: false, used, remaining: 0 };
  }

  // Increment with a 14-day TTL so old keys self-clean
  await kv.put(key, String(used + 1), { expirationTtl: 14 * 24 * 60 * 60 });
  return { allowed: true, used: used + 1, remaining: WEEKLY_LIMIT - used - 1 };
}

/** Generates a short random ID like "cm-a3f9b2c1" */
function makeCardId() {
  return 'cm-' + crypto.randomUUID().replace(/-/g, '').slice(0, 8);
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') ?? '';
    const url    = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // ── Newsletter: GET /confirm?token= ──
    if (request.method === 'GET' && url.pathname === '/confirm') {
      const token = url.searchParams.get('token');
      if (!token) return htmlPage('Invalid Link', '<h1>&#x274C; Invalid Link</h1><p>No token provided.</p><p><a href="https://dizchrisctrl.github.io/daily-digest">Back to digest</a></p>');

      const email = await env.RATE_LIMIT?.get('sub_pending:' + token);
      if (!email) return htmlPage('Link Expired', '<h1>&#x23F0; Link Expired</h1><p>This confirmation link has expired or already been used.</p><p><a href="https://dizchrisctrl.github.io/daily-digest">Sign up again</a></p>');

      const unsubToken = crypto.randomUUID();
      await env.RATE_LIMIT?.put('sub:' + email, JSON.stringify({ confirmed: true, token: unsubToken, subscribedAt: new Date().toISOString() }));
      await env.RATE_LIMIT?.put('sub_unsub:' + unsubToken, email);
      await env.RATE_LIMIT?.delete('sub_pending:' + token);

      const list = await readSubList(env.RATE_LIMIT);
      if (!list.find(s => s.email === email)) {
        list.push({ email, token: unsubToken });
        await writeSubList(env.RATE_LIMIT, list);
      }

      return htmlPage("You're subscribed!", '<h1 style="color:#34d399">&#x2713; You\'re subscribed!</h1><p>You\'ll receive The Daily Rundown every morning.</p><p><a href="https://dizchrisctrl.github.io/daily-digest">Read today\'s digest &#x2192;</a></p>');
    }

    // ── Newsletter: GET /unsubscribe?token= ──
    if (request.method === 'GET' && url.pathname === '/unsubscribe') {
      const token = url.searchParams.get('token');
      if (!token) return htmlPage('Invalid Link', '<h1>&#x274C; Invalid Link</h1><p>No token provided.</p>');

      const email = await env.RATE_LIMIT?.get('sub_unsub:' + token);
      if (!email) return htmlPage('Already Unsubscribed', '<h1>&#x2713; Already Unsubscribed</h1><p>You have already been removed from the list.</p>');

      await env.RATE_LIMIT?.delete('sub:' + email);
      await env.RATE_LIMIT?.delete('sub_unsub:' + token);

      const list = await readSubList(env.RATE_LIMIT);
      await writeSubList(env.RATE_LIMIT, list.filter(s => s.email !== email));

      return htmlPage('Unsubscribed', '<h1>&#x1F44B; You\'ve been unsubscribed</h1><p style="color:#94a3b8">Sorry to see you go. You won\'t receive any more digests.</p><p><a href="https://dizchrisctrl.github.io/daily-digest">Visit the digest</a></p>');
    }

    // ── Newsletter: GET /subscribers (internal, requires WORKER_SECRET) ──
    if (request.method === 'GET' && url.pathname === '/subscribers') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.WORKER_SECRET || auth !== `Bearer ${env.WORKER_SECRET}`) {
        return new Response('Forbidden', { status: 403 });
      }
      const list = await readSubList(env.RATE_LIMIT);
      return new Response(JSON.stringify({ subscribers: list }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // ── GET /card/<id> — retrieve a saved Card Maker card ──
    if (request.method === 'GET') {
      const match = url.pathname.match(/^\/card\/([a-z0-9-]+)$/i);
      if (!match) return new Response('Not Found', { status: 404 });
      const stored = await env.RATE_LIMIT?.get('card:' + match[1]);
      if (!stored) return jsonResponse({ error: 'Card not found or expired.' }, 404, origin);
      return new Response(stored, {
        status:  200,
        headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) },
      });
    }

    // ── Newsletter: POST /subscribe ──
    if (request.method === 'POST' && url.pathname === '/subscribe') {
      let body2;
      try { body2 = await request.json(); } catch { return jsonResponse({ error: 'Invalid JSON.' }, 400, origin); }
      const email = (body2.email || '').trim().toLowerCase();
      if (!isValidEmail(email)) return jsonResponse({ error: 'Invalid email address.' }, 400, origin);

      const existing = await env.RATE_LIMIT?.get('sub:' + email);
      if (existing) {
        const parsed = JSON.parse(existing);
        if (parsed.confirmed) return jsonResponse({ status: 'already_subscribed' }, 200, origin);
      }

      const token = crypto.randomUUID();
      await env.RATE_LIMIT?.put('sub_pending:' + token, email, { expirationTtl: 86400 });

      const confirmUrl = `${url.origin}/confirm?token=${token}`;
      await sendEmail(env.RESEND_API_KEY, {
        from: 'The Daily Rundown <newsletter@resend.dev>',
        to: email,
        subject: 'Confirm your Daily Rundown subscription',
        html: `<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 16px">
          <h2 style="color:#818cf8">Confirm your subscription</h2>
          <p style="color:#94a3b8">Click the button below to confirm you'd like to receive The Daily Rundown digest.</p>
          <a href="${confirmUrl}" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#059669);color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;margin:16px 0">Confirm Subscription</a>
          <p style="color:#475569;font-size:0.8rem">If you didn't sign up, ignore this email. Link expires in 24 hours.</p>
        </div>`,
      });

      return jsonResponse({ status: 'confirmation_sent' }, 200, origin);
    }

    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: { message: 'Invalid JSON body.' } }, 400, origin);
    }

    // ── Mode: get reaction counts for a batch of story IDs ──
    if (body.get_reactions) {
      const ids = Array.isArray(body.ids) ? body.ids.slice(0, 20) : [];
      const result = {};
      await Promise.all(ids.map(async id => {
        const raw = await env.RATE_LIMIT?.get('rxn:' + id);
        result[id] = raw ? JSON.parse(raw) : {};
      }));
      return jsonResponse(result, 200, origin);
    }

    // ── Mode: add a reaction to a story ──
    if (body.add_reaction) {
      const { id, emoji } = body;
      const ALLOWED_EMOJIS = ['\uD83D\uDC4D', '\uD83D\uDC4E', '\uD83E\uDD14', '\uD83D\uDD25', '\uD83D\uDE32']; // 👍 👎 🤔 🔥 😲
      if (!id || !emoji || !ALLOWED_EMOJIS.includes(emoji)) {
        return jsonResponse({ error: 'Invalid reaction.' }, 400, origin);
      }
      const key = 'rxn:' + id;
      const raw  = await env.RATE_LIMIT?.get(key);
      const counts = raw ? JSON.parse(raw) : {};
      counts[emoji] = (counts[emoji] || 0) + 1;
      await env.RATE_LIMIT?.put(key, JSON.stringify(counts));
      return jsonResponse(counts, 200, origin);
    }

    // ── Mode 0: save a generated card to KV ──
    if (body.save_card) {
      if (!env.RATE_LIMIT) return jsonResponse({ error: 'KV not configured.' }, 500, origin);
      const id      = makeCardId();
      const payload = JSON.stringify({ story: body.card, color: body.color || '#f472b6', savedAt: Date.now() });
      await env.RATE_LIMIT.put('card:' + id, payload, { expirationTtl: 30 * 24 * 60 * 60 }); // 30 days
      return jsonResponse({ id }, 200, origin);
    }

    // ── Mode 1: fetch an article URL server-side ──
    if (body.fetch_url) {
      // SSRF guard: only allow public HTTPS URLs
      let fetchUrl;
      try {
        fetchUrl = new URL(body.fetch_url);
      } catch {
        return jsonResponse({ error: 'Invalid fetch_url.' }, 400, origin);
      }
      if (fetchUrl.protocol !== 'https:') {
        return jsonResponse({ error: 'fetch_url must use https.' }, 400, origin);
      }
      const hostname = fetchUrl.hostname.toLowerCase();
      const BLOCKED = /^(localhost|.*\.local|.*\.internal|.*\.corp)$|^(127\.|10\.|192\.168\.|169\.254\.|::1$|fc00:|fe80:)/;
      if (BLOCKED.test(hostname)) {
        return jsonResponse({ error: 'fetch_url targets a private address.' }, 400, origin);
      }
      try {
        const res = await fetch(fetchUrl.toString(), {
          headers: {
            'User-Agent': 'Mozilla/5.0 (compatible; DailyDigestBot/1.0)',
            'Accept':     'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          },
          redirect: 'follow',
          cf: { cacheTtl: 300 },
        });
        if (!res.ok) {
          return jsonResponse({ error: `HTTP ${res.status} fetching article` }, 400, origin);
        }
        const html = await res.text();
        return jsonResponse({ html, url: fetchUrl.toString() }, 200, origin);
      } catch (e) {
        return jsonResponse({ error: e.message }, 500, origin);
      }
    }

    // ── Mode 2: proxy to Anthropic API (rate limited) ──
    const rate = await checkAndIncrement(env.RATE_LIMIT);
    if (!rate.allowed) {
      const resetDay = new Date();
      const daysUntilMonday = (8 - (resetDay.getUTCDay() || 7)) % 7 || 7;
      resetDay.setUTCDate(resetDay.getUTCDate() + daysUntilMonday);
      const resetStr = resetDay.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric', timeZone: 'UTC' });
      return jsonResponse({
        error: {
          message: `Weekly limit of ${WEEKLY_LIMIT} cards reached. Resets ${resetStr}.`,
          type: 'weekly_limit',
          used: rate.used,
          limit: WEEKLY_LIMIT,
        }
      }, 429, origin);
    }

    if (!env.ANTHROPIC_API_KEY) {
      return jsonResponse({ error: { message: 'Worker is missing the ANTHROPIC_API_KEY secret.' } }, 500, origin);
    }

    const upstream = await fetch('https://api.anthropic.com/v1/messages', {
      method:  'POST',
      headers: {
        'Content-Type':      'application/json',
        'x-api-key':         env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify(body),
    });

    const data = await upstream.text();
    return new Response(data, {
      status:  upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') ?? 'application/json',
        ...corsHeaders(origin),
      },
    });
  },
};
