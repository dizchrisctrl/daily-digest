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

function corsHeaders(origin) {
  const allowed = ALLOWED_ORIGINS.find(o => origin.startsWith(o)) ?? ALLOWED_ORIGINS[0];
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

    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: { message: 'Invalid JSON body.' } }, 400, origin);
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
      try {
        const res = await fetch(body.fetch_url, {
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
        return jsonResponse({ html, url: body.fetch_url }, 200, origin);
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
