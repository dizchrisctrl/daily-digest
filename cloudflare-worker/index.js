/**
 * Daily Rundown — Anthropic API proxy + article fetcher
 *
 * Two modes (selected by request body):
 *   { fetch_url: "https://..." }  →  fetches the article server-side, returns { html, url }
 *   { model: "...", ... }         →  proxies request to Anthropic API
 *
 * Deploy:
 *   1. wrangler deploy
 *   2. wrangler secret put ANTHROPIC_API_KEY
 *   3. Add the *.workers.dev URL as WORKER_URL in your GitHub repo secrets
 */

const ALLOWED_ORIGINS = [
  'https://dizchrisctrl.github.io',
  'http://localhost',
  'http://127.0.0.1',
];

function corsHeaders(origin) {
  const allowed = ALLOWED_ORIGINS.find(o => origin.startsWith(o)) ?? ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin':  allowed,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
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

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') ?? '';

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
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

    // ── Mode 1: fetch an article URL server-side ──
    if (body.fetch_url) {
      try {
        const res = await fetch(body.fetch_url, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (compatible; DailyDigestBot/1.0; +https://dizchrisctrl.github.io/daily-digest)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          },
          redirect: 'follow',
          cf: { cacheTtl: 300 },
        });
        if (!res.ok) {
          return jsonResponse({ error: `HTTP ${res.status} from article URL` }, 400, origin);
        }
        const html = await res.text();
        return jsonResponse({ html, url: body.fetch_url }, 200, origin);
      } catch (e) {
        return jsonResponse({ error: e.message }, 500, origin);
      }
    }

    // ── Mode 2: proxy to Anthropic API ──
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
