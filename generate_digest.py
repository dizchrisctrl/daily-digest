#!/usr/bin/env python3
"""Daily Tech & Cybersecurity Digest Generator"""

import os
import re
import json
import base64
import socket
import urllib.parse
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────────
RECIPIENT_EMAIL = "Diazz.christian@gmail.com"
SENDER_EMAIL    = os.environ["GMAIL_ADDRESS"]
PAGES_URL       = "https://dizchrisctrl.github.io/daily-digest"
WORKER_URL      = os.environ.get("WORKER_URL", "")   # Cloudflare Worker proxy URL

GMAIL_CLIENT_ID     = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

# ── RSS Feeds ──────────────────────────────────────────────────────────────────
AI_FEEDS = [
    # Primary — high-frequency AI/ML coverage
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/feed/",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    # Backup — additional high-signal AI sources
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://www.wired.com/feed/category/artificial-intelligence/latest/rss",
    "https://www.zdnet.com/topic/artificial-intelligence/rss.xml",
    "https://feeds.feedburner.com/googleblog",           # Google AI announcements
    "https://openai.com/blog/rss.xml",                   # OpenAI blog
    "https://www.anthropic.com/rss.xml",                 # Anthropic blog
]

CYBER_FEEDS = [
    # Primary — daily security news
    "https://krebsonsecurity.com/feed/",
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.bleepingcomputer.com/feed/",
    "https://isc.sans.edu/rssfeed_full.xml",
    "https://www.darkreading.com/rss.xml",
    # Backup — additional reliable security sources
    "https://feeds.feedburner.com/securityweek",
    "https://nakedsecurity.sophos.com/feed/",            # Sophos Naked Security
    "https://www.schneier.com/blog/atom.xml",            # Schneier on Security
    "https://grahamcluley.com/feed/",                    # Graham Cluley
    "https://www.cisa.gov/news-events/cybersecurity-advisories/feed.xml",  # CISA advisories
    "https://www.csoonline.com/feed/",
]

NOTABLES_FEEDS = [
    # Primary
    "https://www.theverge.com/rss/index.xml",
    "https://www.wired.com/feed/rss",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://hnrss.org/frontpage",
    "https://spectrum.ieee.org/feeds/feed.rss",
    # Backup — broader tech/society coverage
    "https://www.fastcompany.com/technology/rss",
    "https://feeds.a.dj.com/rss/RSSWSJD.xml",           # WSJ Tech
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",  # NYT Tech
    "https://feeds.feedburner.com/TechCrunch",           # TechCrunch broad
    "https://www.technologyreview.com/feed/",
]


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def sanitize_svg(svg):
    """Strip dangerous constructs from Claude-generated SVG before inline embedding."""
    if not svg:
        return ''
    svg = re.sub(r'<script[\s>][\s\S]*?</script\s*>', '', svg, flags=re.IGNORECASE)
    svg = re.sub(r'<foreignObject[\s>][\s\S]*?</foreignObject\s*>', '', svg, flags=re.IGNORECASE)
    svg = re.sub(r'<image[^>]*/?>(?:[\s\S]*?</image>)?', '', svg, flags=re.IGNORECASE)
    svg = re.sub(r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\')', '', svg, flags=re.IGNORECASE)
    svg = re.sub(r'((?:xlink:)?href)\s*=\s*["\']javascript:[^"\']*["\']',
                 r'\1="#"', svg, flags=re.IGNORECASE)
    return svg.strip()


def _single_line(text, max_len=200):
    """Strip newlines/tabs and truncate — for fields that must be single-line in prompts."""
    return re.sub(r'[\r\n\t]+', ' ', strip_html(text or '')).strip()[:max_len]


def _to_eastern(t):
    """Convert a time.struct_time (UTC) from feedparser to an Eastern time string."""
    try:
        import calendar
        utc_dt = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
        # ET = UTC-5 (EST) / UTC-4 (EDT). Use fixed offsets; no pytz needed.
        # DST: second Sunday of March → first Sunday of November
        year = utc_dt.year
        def nth_sunday(month, n):
            d = datetime(year, month, 1)
            d += timedelta(days=(6 - d.weekday()) % 7)
            d += timedelta(weeks=n - 1)
            return d
        dst_start = nth_sunday(3, 2).replace(hour=7, tzinfo=timezone.utc)   # 2am ET = 7am UTC
        dst_end   = nth_sunday(11, 1).replace(hour=6, tzinfo=timezone.utc)  # 2am ET = 6am UTC
        offset = timedelta(hours=-4) if dst_start <= utc_dt < dst_end else timedelta(hours=-5)
        et_dt = utc_dt + offset
        suffix = "EDT" if offset.seconds == 72000 else "EST"  # -4h = 72000s
        return et_dt.strftime(f"%b %d, %Y %-I:%M %p {suffix}").replace("  ", " ")
    except Exception:
        return ""


def _pub_to_utc(pub):
    """Convert feedparser time.struct_time (UTC) to an aware datetime."""
    import calendar
    try:
        return datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
    except Exception:
        return None


def fetch_articles(feeds, max_per_feed=2, total_limit=8, max_age_hours=48):
    """Fetch articles from RSS feeds, keeping only those published within max_age_hours.
    Falls back to 96 hours if fewer than half of total_limit articles are found."""

    def _collect(cutoff):
        seen_titles = set()
        results = []
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(15)
        try:
            for url in feeds:
                try:
                    feed = feedparser.parse(url, request_headers={"User-Agent": "DailyDigest/1.0"})
                    # Sort entries newest-first before taking max_per_feed
                    entries = sorted(
                        feed.entries,
                        key=lambda e: _pub_to_utc(e.get("published_parsed") or e.get("updated_parsed")) or datetime.min.replace(tzinfo=timezone.utc),
                        reverse=True,
                    )
                    count = 0
                    for entry in entries:
                        if count >= max_per_feed:
                            break
                        pub = entry.get("published_parsed") or entry.get("updated_parsed")
                        pub_dt = _pub_to_utc(pub) if pub else None
                        # Skip articles outside the recency window
                        if pub_dt and pub_dt < cutoff:
                            continue
                        title = _single_line(entry.get("title", "Untitled"))
                        # Deduplicate by normalised title
                        title_key = re.sub(r'\W+', '', title.lower())[:60]
                        if title_key in seen_titles:
                            continue
                        seen_titles.add(title_key)
                        link = entry.get("link", "")
                        if not str(link).lower().startswith(("http://", "https://")):
                            link = ""
                        summary = strip_html(entry.get("summary", entry.get("description", "")))[:600]
                        results.append({
                            "title":    title,
                            "summary":  summary,
                            "link":     link,
                            "source":   _single_line(feed.feed.get("title", "Unknown Source")),
                            "pub_date": _to_eastern(pub) if pub else "",
                            "_pub_dt":  pub_dt,
                        })
                        count += 1
                except Exception as e:
                    print(f"  Feed error [{url}]: {e}")
        finally:
            socket.setdefaulttimeout(old_timeout)
        # Sort all collected articles newest-first
        results.sort(key=lambda a: a.get("_pub_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return results

    now = datetime.now(timezone.utc)
    articles = _collect(now - timedelta(hours=max_age_hours))

    if len(articles) < max(2, total_limit // 2):
        print(f"  ⚠ Only {len(articles)} articles in {max_age_hours}h window — widening to 96h")
        articles = _collect(now - timedelta(hours=96))

    # Strip internal sort key before returning
    for a in articles:
        a.pop("_pub_dt", None)

    print(f"  Fetched {len(articles[:total_limit])} articles (within recency window)")
    return articles[:total_limit]


# ── Story schema (AI + Cyber deep-dive cards) ──────────────────────────────────
STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":          {"type": "string", "minLength": 1, "description": "Short punchy headline — required, never empty"},
        "pub_date":          {"type": "string", "description": "Publication date of the source article — copy exactly from the article's pub_date field"},
        "tldr":              {"type": "string", "minLength": 1, "description": "One sentence that tells the whole story"},
        "why_it_matters":    {"type": "string", "description": "2-3 sentences on real-world significance"},
        "concept_title":     {"type": "string", "description": "The core technical concept illustrated"},
        "concept_explained": {"type": "string", "description": "4 paragraphs separated by newlines. P1: simple real-world analogy. P2: how it technically works. P3: tie to this news story. P4: broader implications."},
        "visual_svg": {
            "type": "string",
            "description": (
                "A complete <svg> element with viewBox=\"0 0 700 340\". No external refs, no scripts, no event handlers. "
                "Choose the diagram type that best illuminates this concept: "
                "attack chain (sequential labelled steps + arrows), "
                "architecture (component boxes + directed edges), "
                "data flow (requests/data moving through a system), "
                "timeline (horizontal sequence of events), "
                "or comparison (side-by-side columns). "
                "Styling: background <rect fill=\"#060912\"/>, "
                "node boxes fill=\"#12152a\" stroke=\"#252840\" rx=\"6\", "
                "key nodes stroke=\"__ACCENT__\", "
                "arrowheads via <defs><marker> fill=\"__ACCENT__\", "
                "primary labels fill=\"#eaedf5\" font-size=\"13\" font-family=\"monospace\", "
                "secondary labels fill=\"#7a849a\" font-size=\"11\", "
                "accent callouts fill=\"__ACCENT__\" with fill=\"#060912\" text. "
                "Produce 8-15 elements. Short precise labels. Show actual directional flow — not just floating labelled boxes."
            ),
        },
        "public_opinion": {
            "type": "array",
            "description": "Concrete sentiments from HN, Reddit, security Twitter — one entry per source",
            "items": {
                "type": "object",
                "properties": {
                    "source":    {"type": "string", "description": "Community name, e.g. 'Hacker News', 'Reddit r/netsec', 'Security Twitter'"},
                    "sentiment": {"type": "string", "description": "1-2 sentence summary of what that community is saying"},
                },
                "required": ["source", "sentiment"],
            },
        },
        "opinion_assessment":{"type": "string", "description": "2-3 sentences summarizing the overall collective sentiment across all communities — what is the dominant mood, the shared concern or excitement, and the key theme running through all the reactions"},
        "devils_advocate":   {"type": "string", "description": "A sharp, provocative counter-perspective that challenges the dominant public sentiment. Reveal an overlooked irony, an inconvenient truth, or a reframe that makes the reader stop and think differently about the story. Should feel like a genuine twist, not a mild qualification."},
        "quiz": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lens":    {"type": "string", "enum": ["Scientific", "Historical", "Societal"], "description": "The perspective lens for this insight card — assigned in order: card 1 = Scientific, card 2 = Historical, card 3 = Societal"},
                    "q":       {"type": "string", "description": "A thought-provoking hook or framing question through the assigned lens — not a trivia question"},
                    "a":       {"type": "string", "description": "The key insight or takeaway: a crisp, memorable answer that reframes or deepens understanding"},
                    "explain": {"type": "string", "description": "2-3 sentences connecting this insight to broader trends, historical context, or implications beyond the story — stay within the assigned lens"},
                },
                "required": ["lens", "q", "a", "explain"],
            },
            "minItems": 3,
            "maxItems": 3,
        },
        "deep_dive":  {"type": "string", "description": "A riveting 3-4 sentence narrative that synthesizes everything in the story — the concept, the event, the opinions, the stakes — into a single compelling thread. Write it like the opening of a great longform piece: draw the reader in, raise the tension, and leave them wanting more."},
        "deep_dive_impact":  {"type": "string", "description": "2-3 sentences on how this story directly affects the reader — their day-to-day work, the tools they use, their security posture, or their career trajectory. Be specific and personal, not generic."},
        "deep_dive_outlook": {"type": "string", "description": "A 2-3 sentence forward-looking conclusion: what is likely to happen next, what trends this story accelerates or disrupts, and what to watch for in the coming weeks or months. Grounded and specific — avoid vague generalities."},
        "source_url": {"type": "string"},
        "source":     {"type": "string"},
        "tech_tags": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Specific product, framework, protocol, or CVE ID — never generic terms like 'AI', 'cloud', or 'security'"},
                    "description": {"type": "string", "description": "1-2 sentences: what this technology or system is"},
                    "relevance":   {"type": "string", "description": "1-2 sentences: its specific role in this story"},
                },
                "required": ["name", "description", "relevance"],
            },
            "description": "0-3 tags max. ONLY include when you can supply specific, meaningful context. For vulnerability stories skip the tag entirely if no specific version or tool is confirmed. Omit generic terms.",
        },
        "affected_systems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string", "description": "Application or system name, e.g. 'Apache Log4j'"},
                    "versions": {"type": "string", "description": "Affected version range, e.g. '2.0-beta9 to 2.14.1' or 'all versions before 2.17.0'"},
                },
                "required": ["name", "versions"],
            },
            "description": "For vulnerability/CVE stories: list each affected system with its version range. Use an empty array for non-vulnerability stories.",
        },
    },
    "required": ["headline","pub_date","tldr","why_it_matters","concept_title","concept_explained",
                 "visual_svg","public_opinion","opinion_assessment","devils_advocate","quiz","deep_dive",
                 "deep_dive_impact","deep_dive_outlook","source_url","source","tech_tags","affected_systems"],
}

SECTION_TOOL = {
    "name": "publish_story",
    "description": "Publish one fully formatted digest story",
    "input_schema": STORY_SCHEMA,
}

SECTION_PROMPT = """Today is {today}. Write ONE digest story about the article below for someone moderately technical — works in or near tech/security, understands concepts, appreciates clear explanations with real depth.

ARTICLE:
{article}

Story number {story_num} of 3 for this section.

Guidelines:
- concept_explained: 4 paragraphs. P1: simple real-world analogy. P2: how it technically works. P3: tie to this story. P4: broader implications.
- visual_svg: SVG diagram (viewBox="0 0 700 340"). Pick the right type for the concept:
    attack chain → boxes left-to-right with labelled arrows showing each step
    architecture → nodes with directed edges showing components and data flow
    data flow → directional arrows tracing a request or signal through a system
    timeline → horizontal bar with labelled events/milestones
    comparison → two columns (e.g. before/after, secure/insecure)
  Use accent color {accent_color}. Dark background #060912. Node fill #12152a.
  Include arrowheads via <defs><marker>. 8-15 elements. Short precise labels.
  Show actual relationships and flow — not floating boxes with buzzwords.
- quiz: 3 insight cards, each written through a distinct lens — card 1: Scientific (how it works, engineering tradeoffs, what it advances or breaks), card 2: Historical (what precedent this echoes, what the pattern tells us, what we've seen before), card 3: Societal (how it affects people beyond the technical community — policy, economics, behavior, power). Each card has a hook (q) framed through its lens, a crisp key insight (a), and an explanation (explain) that stays within the lens. Avoid trivia — these should feel like genuine "aha" moments.
- pub_date: copy the pub_date field exactly from the article JSON — do not modify it.
- public_opinion: one entry per community (HN, Reddit r/technology, r/netsec, security Twitter/X) — each with a source name and 1-2 sentence sentiment summary.
- opinion_assessment: 2-3 sentences capturing the dominant collective mood across all communities. What is everyone feeling, and why?
- devils_advocate: challenge the dominant sentiment with a sharp counter-perspective — an overlooked irony, an inconvenient truth, or a reframe that makes the reader reconsider the story. Make it feel like a genuine twist, not a mild qualification.
- deep_dive: 3-4 sentences that synthesize the full story — concept, event, stakes, and tensions — into a compelling narrative thread. Write like the opening of great longform journalism: draw the reader in, raise the tension, leave them wanting more.
- deep_dive_impact: 2-3 sentences on how this directly affects the reader — their work, their tools, their security posture, or their career. Be specific and personal.
- deep_dive_outlook: 2-3 sentences on what happens next — what this story likely accelerates or disrupts, what to watch in the coming weeks or months. Grounded and specific, not vague.
- tech_tags: 0-3 tags max. Only include when you have specific, meaningful context to share. Skip entirely for vulnerabilities if no specific version or tool is confirmed. Never use generic terms (AI, cloud, encryption). Each tag needs a clear description and a relevance sentence tied to this exact story.
- affected_systems: for vulnerability stories, list each affected system with its version range. Empty array otherwise.

Call the publish_story tool with your story."""


# ── Notables schema (broader news highlights with applicability) ───────────────
NOTABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":      {"type": "string", "minLength": 1, "description": "Short punchy headline — required, never empty"},
        "summary":       {"type": "string", "description": "2-3 sentences covering what happened and why it is significant"},
        "applicability": {"type": "string", "description": "2-3 sentences on how this could matter to someone in tech or security — career implications, tools to watch, policy awareness, market shifts"},
        "category":      {"type": "string", "description": "One of: Policy, Business, Research, Infrastructure, Society, Science"},
        "source_url":    {"type": "string"},
        "source":        {"type": "string"},
        "tech_tags": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Specific product, company, or technology name — no generic terms"},
                    "description": {"type": "string", "description": "1-2 sentences: what this is"},
                    "relevance":   {"type": "string", "description": "1-2 sentences: its role in this story"},
                },
                "required": ["name", "description", "relevance"],
            },
            "description": "0-3 tags. Only include when genuinely specific and informative. Empty array is fine.",
        },
    },
    "required": ["headline", "summary", "applicability", "category", "source_url", "source", "tech_tags"],
}

NOTABLES_TOOL = {
    "name": "publish_notables",
    "description": "Publish 5 notable news highlights with applicability notes",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": NOTABLE_SCHEMA, "minItems": 5, "maxItems": 5},
        },
        "required": ["items"],
    },
}

NOTABLES_PROMPT = """Today is {today}. Select 5 notable tech and world stories that someone in tech or security should be aware of. Prefer stories outside of pure AI model news or cybersecurity incidents — those are covered in separate sections. Focus on policy, business shifts, research breakthroughs, infrastructure, society, and science stories with real downstream relevance.

NEWS ARTICLES:
{articles}

For each item:
- summary: 2-3 sentences on what happened
- applicability: 2-3 sentences connecting to real implications for someone in tech/security (career, tools, policy awareness, market shifts)
- category: Policy | Business | Research | Infrastructure | Society | Science
- tech_tags: 0-3 tags max. Only include when genuinely specific. Empty array is perfectly fine.

Prioritize stories with genuine weight. Avoid minor product launches or clickbait.
Call the publish_notables tool with your 5 items."""


# ── Claude calls ───────────────────────────────────────────────────────────────
def _extract_tool_input(response, key, call_label):
    """Pull from the first tool_use block.

    If key is None, return the entire input dict (for single-object schemas).
    If key is a string, return input[key].
    Raises RuntimeError with diagnostic info on any failure.
    """
    if not response.content:
        raise RuntimeError(f"{call_label}: empty response content (stop_reason={response.stop_reason})")
    block = response.content[0]
    if not hasattr(block, "input"):
        raise RuntimeError(
            f"{call_label}: expected tool_use block, got {type(block).__name__} "
            f"(stop_reason={response.stop_reason})"
        )
    if key is None:
        if not block.input:
            raise RuntimeError(
                f"{call_label}: tool input is empty. stop_reason={response.stop_reason}"
            )
        return block.input
    if key not in block.input:
        available = list(block.input.keys())
        raise RuntimeError(
            f"{call_label}: tool input missing key '{key}'. "
            f"Available keys: {available}. stop_reason={response.stop_reason}"
        )
    return block.input[key]


def call_claude_for_section(client, today, articles, accent_color="#818cf8"):
    """Generate 3 stories one at a time to stay within the 8192-token output limit."""
    # Ask Claude to pick the 3 most notable articles first, then generate one story each
    top_articles = articles[:8]  # give it up to 8 to choose from
    stories = []
    # Use a simple selection pass to pick the 3 best articles
    selection_prompt = f"""Today is {today}. From these articles, pick the 3 most notable and return their indices (0-based) as a JSON array. Prefer stories with real technical depth or significant impact.

ARTICLES:
{json.dumps([{"i": i, "title": a.get("title",""), "source": a.get("source","")} for i, a in enumerate(top_articles)], indent=2)}

Reply with only a JSON array of 3 indices, e.g.: [0, 2, 5]"""
    sel_response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=50,
        messages=[{"role": "user", "content": selection_prompt}],
    )
    try:
        indices = json.loads(sel_response.content[0].text.strip())
        chosen = [top_articles[i] for i in indices if i < len(top_articles)][:3]
    except Exception:
        chosen = top_articles[:3]  # fallback: just take first 3

    for story_num, article in enumerate(chosen, start=1):
        prompt = SECTION_PROMPT.format(
            today=today,
            article=json.dumps(article, indent=2),
            accent_color=accent_color,
            story_num=story_num,
        )
        for attempt in range(1, 3):
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=8000,
                tools=[SECTION_TOOL],
                tool_choice={"type": "tool", "name": "publish_story"},
                messages=[{"role": "user", "content": prompt}],
            )
            try:
                story = _extract_tool_input(response, None, "call_claude_for_section")
                stories.append(story)
                break
            except RuntimeError as exc:
                if attempt == 2:
                    raise
                print(f"  WARNING: story {story_num} attempt {attempt} failed ({exc}), retrying...")
    return stories


def call_claude_for_notables(client, today, articles):
    prompt = NOTABLES_PROMPT.format(today=today, articles=json.dumps(articles, indent=2))
    for attempt in range(1, 3):
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4000,
            tools=[NOTABLES_TOOL],
            tool_choice={"type": "tool", "name": "publish_notables"},
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            return _extract_tool_input(response, "items", "call_claude_for_notables")
        except RuntimeError as exc:
            if attempt == 2:
                raise
            print(f"  WARNING: attempt {attempt} failed ({exc}), retrying...")


def generate_digest_json(ai_articles, cyber_articles, notables_articles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now(timezone.utc).strftime("%B %d, %Y")

    print("  -> Generating AI stories...")
    ai_stories = call_claude_for_section(client, today, ai_articles, accent_color="#818cf8")
    print("  -> Generating Cybersecurity stories...")
    cyber_stories = call_claude_for_section(client, today, cyber_articles, accent_color="#34d399")
    print("  -> Generating Notables...")
    notables = call_claude_for_notables(client, today, notables_articles)

    return {"date": today, "ai_stories": ai_stories, "cyber_stories": cyber_stories, "notables": notables}


# ── HTML Template ──────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'none'; connect-src __CONNECT_SRC__; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none';">
<title>The Daily Rundown -- __DATE__</title>
<meta property="og:type"        content="website">
<meta property="og:site_name"   content="The Daily Rundown">
<meta property="og:title"       content="__OG_TITLE__">
<meta property="og:description" content="__OG_DESC__">
<meta property="og:url"         content="__OG_URL__">
<meta name="twitter:card"        content="summary">
<meta name="twitter:title"       content="__OG_TITLE__">
<meta name="twitter:description" content="__OG_DESC__">
<style>
:root {
  --bg: #0b0d16; --surface: #12152a; --surface2: #1a1d32; --surface3: #20233c;
  --text: #eaedf5; --muted: #7a849a; --muted2: #5a6275;
  --ai: #818cf8; --ai2: #6366f1; --cyber: #34d399; --cyber2: #10b981;
  --notables: #fbbf24; --notables2: #f59e0b;
  --purple: #a78bfa; --amber: #fbbf24;
  --border: #252840; --border2: #333660;
  --glow-ai: rgba(129,140,248,0.15); --glow-cyber: rgba(52,211,153,0.15);
  --glow-notables: rgba(251,191,36,0.12);
  --header-bg: linear-gradient(180deg, #161928 0%, var(--bg) 100%);
  --header-glow: rgba(129,140,248,0.12);
  --body-text: #c0c8d8;
  --concept-bg: linear-gradient(180deg, #171b30 0%, #141828 100%);
  --diagram-bg: #060912; --diagram-bar-bg: #0d1020; --diagram-border: #1e3055;
  --diagram-title-color: #3a4a60;
  --opinion-bg: #0d160e;
  --deepdive-bg: linear-gradient(135deg, #110d22 0%, #0e0c1e 100%);
  --deepdive-text-color: #c4b5fd;
  --deepdive-quote-color: rgba(167,139,250,0.07);
}
html.light {
  --bg: #f4f6fb; --surface: #ffffff; --surface2: #eef0f7; --surface3: #e4e7f2;
  --text: #1a1d2e; --muted: #4a5568; --muted2: #718096;
  --ai: #4f46e5; --ai2: #4338ca; --cyber: #059669; --cyber2: #047857;
  --notables: #d97706; --notables2: #b45309;
  --purple: #7c3aed; --amber: #d97706;
  --border: #d4d8ec; --border2: #c0c5df;
  --glow-ai: rgba(79,70,229,0.08); --glow-cyber: rgba(5,150,105,0.08);
  --glow-notables: rgba(217,119,6,0.08);
  --header-bg: linear-gradient(180deg, #e8ecf8 0%, var(--bg) 100%);
  --header-glow: rgba(79,70,229,0.08);
  --body-text: #2d3748;
  --concept-bg: linear-gradient(180deg, #eef0f7 0%, #e8ebf5 100%);
  --diagram-bg: #f0f2fa; --diagram-bar-bg: #e4e7f2; --diagram-border: #c0c5df;
  --diagram-title-color: #6b7a99;
  --opinion-bg: #edf7f2;
  --deepdive-bg: linear-gradient(135deg, #f0edf8 0%, #ece8f5 100%);
  --deepdive-text-color: #5b21b6;
  --deepdive-quote-color: rgba(124,58,237,0.08);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.7; min-height: 100vh; }

/* ── Progress Bar ── */
#progress-bar {
  position: fixed; top: 0; left: 0; height: 3px; width: 0%;
  background: linear-gradient(90deg, var(--ai), var(--purple), var(--notables));
  z-index: 9999; transition: width 0.08s linear;
  box-shadow: 0 0 10px rgba(251,191,36,0.4);
}

/* ── Header ── */
.site-header {
  padding: 44px 20px 32px;
  text-align: center;
  position: relative;
  overflow: hidden;
  border-bottom: 1px solid var(--border);
  background: var(--header-bg);
}
.site-header::before {
  content: '';
  position: absolute; inset: 0;
  background: radial-gradient(ellipse 70% 60% at 50% 0%, var(--header-glow) 0%, transparent 70%);
  pointer-events: none;
}
.eyebrow { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 4px; color: var(--muted2); margin-bottom: 14px; }
.site-header h1 {
  font-size: 2.8rem; font-weight: 900; letter-spacing: -2px; line-height: 1;
  margin-bottom: 14px;
}
/* ── Theme toggle ── */
.theme-toggle {
  position: absolute; top: 16px; right: 16px;
  background: var(--surface2); border: 1px solid var(--border2);
  color: var(--muted); border-radius: 20px; padding: 5px 12px;
  font-size: 0.78rem; cursor: pointer; display: flex; align-items: center; gap: 6px;
  transition: background 0.2s, color 0.2s, border-color 0.2s;
}
.theme-toggle:hover { background: var(--surface3); color: var(--text); }
.header-tagline {
  font-size: 0.88rem; color: var(--muted); max-width: 480px; margin: 0 auto 16px;
  line-height: 1.6; font-style: italic;
}
.date-badge {
  display: inline-flex; align-items: center; gap: 8px;
  color: var(--muted); font-size: 0.82rem; padding: 5px 16px;
  border: 1px solid var(--border2); border-radius: 20px;
  background: var(--surface2);
}
.guide-link {
  display: inline-block; margin-top: 14px;
  font-size: 0.78rem; color: var(--muted2); text-decoration: none;
  border: 1px solid var(--border); border-radius: 20px; padding: 4px 14px;
  transition: color 0.2s, border-color 0.2s;
}
.guide-link:hover { color: var(--ai); border-color: var(--ai); }

/* ── Animated title ── */
@keyframes shimmer {
  0%   { background-position: -200% center; }
  100% { background-position: 200% center; }
}
@keyframes title-in {
  0%   { opacity: 0; transform: translateY(18px) scale(0.97); filter: blur(6px); }
  100% { opacity: 1; transform: translateY(0) scale(1);       filter: blur(0); }
}
@keyframes glow-pulse {
  0%, 100% { text-shadow: 0 0 40px rgba(129,140,248,0.0); }
  50%       { text-shadow: 0 0 60px rgba(129,140,248,0.25), 0 0 120px rgba(167,139,250,0.15); }
}
.site-header h1 {
  background: linear-gradient(130deg, var(--ai) 0%, var(--purple) 30%, var(--cyber) 60%, var(--ai) 100%);
  background-size: 300% auto;
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  animation: title-in 0.8s cubic-bezier(0.22,1,0.36,1) both,
             shimmer 4s linear 0.8s infinite,
             glow-pulse 3s ease-in-out 0.8s infinite;
}
.date-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--cyber); display: inline-block; }

/* ── Tabs ── */
.tabs-wrap { background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 100; backdrop-filter: blur(12px); }
.tabs { display: flex; max-width: 860px; margin: 0 auto; padding: 0 16px; position: relative; }
.tab-btn {
  flex: 1; padding: 15px 12px; background: none; border: none; border-bottom: 2px solid transparent;
  color: var(--muted); font-size: 0.88rem; font-weight: 600; cursor: pointer;
  transition: color 0.2s; display: flex; align-items: center; justify-content: center; gap: 7px;
}
.tab-btn.active { color: var(--text); }
.tab-indicator {
  position: absolute; bottom: 0; height: 2px;
  background: var(--ai); border-radius: 2px 2px 0 0;
  transition: left 0.3s cubic-bezier(0.4,0,0.2,1), width 0.3s cubic-bezier(0.4,0,0.2,1), background 0.3s;
}

/* ── Content ── */
.content { max-width: 860px; margin: 0 auto; padding: 28px 16px 80px; }
.section { display: none; }
.section.active { display: block; }

/* Section bar */
.section-bar {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid var(--border);
}
.section-label { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--muted2); }
.expand-all {
  font-size: 0.72rem; font-weight: 600; color: var(--muted);
  background: var(--surface2); border: 1px solid var(--border);
  padding: 4px 12px; border-radius: 20px; cursor: pointer;
  transition: color 0.2s, border-color 0.2s;
}
.expand-all:hover { color: var(--text); border-color: var(--border2); }

/* ── Story Card ── */
@keyframes storyHighlight {
  0%   { box-shadow: 0 0 0 0 var(--accent,#818cf8), 0 8px 32px rgba(0,0,0,0.35); }
  18%  { box-shadow: 0 0 0 4px var(--accent,#818cf8), 0 0 40px color-mix(in srgb, var(--accent,#818cf8) 30%, transparent), 0 8px 32px rgba(0,0,0,0.35); }
  55%  { box-shadow: 0 0 0 3px var(--accent,#818cf8), 0 0 24px color-mix(in srgb, var(--accent,#818cf8) 18%, transparent), 0 8px 32px rgba(0,0,0,0.35); }
  100% { box-shadow: 0 0 0 0 transparent, 0 8px 32px rgba(0,0,0,0.35); }
}
.story-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  margin-bottom: 12px;
  overflow: hidden;
  transition: border-color 0.25s, box-shadow 0.25s;
  scroll-margin-top: 80px;
}
.story-card:hover { border-color: var(--border2); box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
.story-card.open { border-color: var(--border2); box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
.story-card.kbd-focus { border-color: var(--ai) !important; box-shadow: 0 0 0 2px rgba(129,140,248,0.25) !important; }
.story-card.story-highlight { animation: storyHighlight 2.4s cubic-bezier(0.4,0,0.2,1) forwards; }

/* Summary row */
.story-summary {
  padding: 20px 22px; cursor: pointer;
  display: flex; align-items: flex-start; gap: 14px;
  transition: background 0.15s; user-select: none;
}
.story-summary:hover { background: rgba(255,255,255,0.025); }
.s-left { flex: 1; min-width: 0; }
.s-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 9px; }
.src-badge { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; padding: 2px 9px; border-radius: 20px; }
.story-num { font-size: 0.68rem; font-weight: 700; color: var(--muted2); font-variant-numeric: tabular-nums; margin-left: auto; }
.read-time { font-size: 0.68rem; color: var(--muted2); display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; }
.pub-date { font-size: 0.72rem; color: var(--muted2); margin: 4px 0 8px; }
.story-summary h2 { font-size: 1.08rem; font-weight: 700; line-height: 1.4; margin-bottom: 9px; }
.tldr { font-size: 0.88rem; color: var(--muted); line-height: 1.65; }
.tldr-tag {
  display: inline-block; font-size: 0.6rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--muted2); background: var(--surface3);
  padding: 1px 6px; border-radius: 4px; margin-right: 6px; vertical-align: middle;
}
.chevron {
  flex-shrink: 0; width: 28px; height: 28px; border-radius: 50%;
  border: 1px solid var(--border); display: flex; align-items: center; justify-content: center;
  color: var(--muted2); font-size: 0.65rem; margin-top: 2px;
  transition: transform 0.35s cubic-bezier(0.4,0,0.2,1), background 0.2s, border-color 0.2s, color 0.2s;
}
.story-card.open .chevron { transform: rotate(180deg); background: var(--surface3); border-color: var(--border2); color: var(--muted); }

/* Expandable body */
.story-body { max-height: 0; overflow: hidden; transition: max-height 0.5s cubic-bezier(0.4,0,0.2,1); }
.story-card.open .story-body { max-height: 8000px; transition: max-height 0.9s cubic-bezier(0,0,0.2,1); }
.body-inner { border-top: 1px solid var(--border); }

/* Blocks */
.block { padding: 20px 22px; border-top: 1px solid var(--border); }
.block:first-child { border-top: none; }
.blabel { font-size: 0.67rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--muted2); margin-bottom: 11px; }
.block p { font-size: 0.93rem; line-height: 1.75; color: var(--body-text); }

/* Concept block */
.concept-block { background: var(--concept-bg); }
.concept-title { font-size: 0.97rem; font-weight: 700; margin-bottom: 14px; }
.concept-text p { font-size: 0.93rem; line-height: 1.8; color: var(--body-text); margin-bottom: 13px; }
.concept-text p:last-child { margin-bottom: 0; }

/* ── SVG Diagram ── */
.diagram-wrap { border-radius: 10px; overflow: hidden; border: 1px solid var(--diagram-border); background: var(--diagram-bg); }
.diagram-bar { background: var(--diagram-bar-bg); padding: 9px 14px; display: flex; align-items: center; gap: 7px; border-bottom: 1px solid var(--diagram-border); }
.dot { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }
.dot-r { background: #ff5f57; } .dot-y { background: #febc2e; } .dot-g { background: #28c840; }
.diagram-title { flex: 1; text-align: center; font-size: 0.67rem; color: var(--diagram-title-color); font-family: monospace; }
.diagram-svg { display: block; }
.diagram-svg svg { width: 100%; height: auto; display: block; }
/* ASCII fallback for --rebuild with old digest.json */
pre.ascii {
  font-family: 'Courier New', Courier, monospace;
  font-size: 0.74rem; line-height: 1.5;
  color: var(--ai); padding: 16px 18px;
  background: var(--diagram-bg); overflow-x: auto; white-space: pre;
}

/* Opinion block */
.opinion-block { background: var(--opinion-bg); }
details.opinion-entry { margin-bottom: 8px; border-left: 3px solid rgba(52,211,153,0.4); border-radius: 0 6px 6px 0; background: rgba(52,211,153,0.04); }
details.opinion-entry:last-of-type { margin-bottom: 0; }
details.opinion-entry summary {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 14px; cursor: pointer; list-style: none;
  user-select: none;
}
details.opinion-entry summary::-webkit-details-marker { display: none; }
.opinion-chevron { font-size: 0.65rem; color: var(--muted2); transition: transform 0.2s; flex-shrink: 0; }
details.opinion-entry[open] .opinion-chevron { transform: rotate(90deg); }
.opinion-source { font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--cyber); flex-shrink: 0; }
.opinion-preview { font-size: 0.82rem; color: var(--muted2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.opinion-full { padding: 0 14px 11px 14px; font-size: 0.91rem; color: var(--muted); line-height: 1.6; }

/* Insights */
.insights-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
.qcard {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px; cursor: pointer;
  transition: border-color 0.2s, transform 0.15s, box-shadow 0.15s;
  user-select: none;
}
.qcard:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.3); border-color: var(--border2); }
.qcard.open { border-color: var(--cyber); }
.q-num { font-size: 0.63rem; font-weight: 700; color: var(--muted2); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 7px; display: flex; align-items: center; gap: 6px; }
.q-lens { font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; padding: 2px 6px; border-radius: 4px; }
.q-lens-scientific { background: rgba(129,140,248,0.15); color: #818cf8; }
.q-lens-historical  { background: rgba(251,191,36,0.15);  color: #fbbf24; }
.q-lens-societal    { background: rgba(52,211,153,0.15);  color: #34d399; }
.q-text { font-weight: 600; font-size: 0.87rem; line-height: 1.5; color: var(--text); }
.q-answer { max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.35s ease, opacity 0.3s ease; }
.qcard.open .q-answer { max-height: 400px; opacity: 1; }
.q-divider { height: 1px; background: var(--border); margin: 10px 0; }
.q-ans { color: var(--cyber); font-weight: 600; font-size: 0.85rem; margin-bottom: 5px; }
.q-exp { color: var(--muted); font-style: italic; font-size: 0.79rem; line-height: 1.55; }
.q-hint { font-size: 0.65rem; color: var(--muted2); margin-top: 8px; display: flex; align-items: center; gap: 4px; }
.qcard.open .q-hint { color: var(--cyber2); }

/* Devil's Advocate */
.devil-block { background: rgba(239,68,68,0.04); border-left: 3px solid rgba(239,68,68,0.5) !important; }
.devil-intro { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: #ef4444; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
.devil-text { font-size: 0.94rem; color: var(--muted); line-height: 1.7; font-style: italic; }

/* Deep Dive */
.deepdive-block { background: var(--deepdive-bg); position: relative; overflow: hidden; }
.deepdive-block::after { content: '"'; position: absolute; right: 18px; top: 8px; font-size: 6rem; color: var(--deepdive-quote-color); font-family: Georgia, serif; line-height: 1; }
.deepdive-text { font-size: 1.02rem; font-style: italic; color: var(--deepdive-text-color); padding-left: 16px; border-left: 3px solid var(--purple); line-height: 1.85; }
.deepdive-impact { margin-top: 18px; padding: 14px 16px; background: rgba(139,92,246,0.07); border-radius: 8px; border: 1px solid rgba(139,92,246,0.2); }
.deepdive-impact-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--purple); margin-bottom: 6px; }
.deepdive-impact-text { font-size: 0.92rem; color: var(--muted); line-height: 1.7; }
.deepdive-outlook { margin-top: 14px; padding: 14px 16px; background: rgba(251,191,36,0.05); border-radius: 8px; border: 1px solid rgba(251,191,36,0.18); }
.deepdive-outlook-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--amber); margin-bottom: 6px; }
.deepdive-outlook-text { font-size: 0.92rem; color: var(--muted); line-height: 1.7; }

/* ── Collapsible blocks ── */
.collapsible-head { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.collapsible-head .blabel,
.collapsible-head .devil-intro,
.collapsible-head .deepdive-impact-label,
.collapsible-head .deepdive-outlook-label { margin-bottom: 0; pointer-events: none; flex-shrink: 0; }
.collapsible-preview { font-size: 0.8rem; color: var(--muted2); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-style: italic; min-width: 0; transition: opacity 0.15s ease, visibility 0.15s ease; }
.collapsible.open .collapsible-preview { opacity: 0; visibility: hidden; pointer-events: none; }
.collapsible-chevron { font-size: 0.65rem; color: var(--muted2); flex-shrink: 0; transition: transform 0.3s cubic-bezier(0.4,0,0.2,1); line-height: 1; }
.collapsible-head:hover .collapsible-chevron { color: var(--accent, #818cf8); }
.collapsible.open .collapsible-chevron { transform: rotate(90deg); }
.collapsible-body { overflow: hidden; max-height: 0; opacity: 0; margin-top: 0; transition: max-height 0.4s cubic-bezier(0.4,0,0.2,1), opacity 0.3s ease, margin-top 0.25s ease; }
.collapsible.open .collapsible-body { max-height: 1600px; opacity: 1; margin-top: 11px; }

/* Audio player */
.audio-row { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.audio-btn {
  display: inline-flex; align-items: center; gap: 5px;
  background: transparent; border: 1px solid var(--border2); color: var(--muted);
  border-radius: 20px; padding: 4px 13px; font-size: 0.73rem; font-family: inherit;
  cursor: pointer; transition: border-color 0.2s, color 0.2s; user-select: none;
}
.audio-btn:hover { border-color: var(--ai); color: var(--ai); }
.audio-btn.au-playing { border-color: var(--cyber); color: var(--cyber); }
.audio-btn.au-paused  { border-color: var(--amber); color: var(--amber); }
.audio-stop {
  display: none; background: transparent; border: 1px solid var(--border);
  color: var(--muted2); border-radius: 20px; padding: 4px 11px;
  font-size: 0.73rem; font-family: inherit; cursor: pointer;
  transition: border-color 0.2s, color 0.2s;
}
.audio-stop:hover { border-color: var(--red, #f87171); color: var(--red, #f87171); }
.audio-stop.au-visible { display: inline-block; }
.audio-status { font-size: 0.7rem; color: var(--cyber); display: none; align-items: center; gap: 5px; }
.audio-status.au-visible { display: flex; }
.audio-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--cyber); flex-shrink: 0; }
.au-playing .audio-dot { animation: au-pulse 1.1s ease-in-out infinite; }
@keyframes au-pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:0.3; transform:scale(0.7); } }

/* Share */
.share-wrap { position: relative; display: inline-block; }
.share-btn {
  display: inline-flex; align-items: center; gap: 5px;
  background: transparent; border: 1px solid var(--border2); color: var(--muted);
  border-radius: 20px; padding: 4px 13px; font-size: 0.73rem; font-family: inherit;
  cursor: pointer; transition: border-color 0.2s, color 0.2s; user-select: none;
}
.share-btn:hover { border-color: var(--purple); color: var(--purple); }
.share-btn.share-active { border-color: var(--purple); color: var(--purple); }
.share-popover {
  display: none; position: absolute; top: calc(100% + 10px); right: 0;
  background: var(--surface2); border: 1px solid var(--border2);
  border-radius: 14px; padding: 12px; min-width: 220px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.45); z-index: 200;
}
.share-popover.open { display: block; }
/* Popover opens upward when at the bottom of the panel */
.share-popover-up {
  top: auto; bottom: calc(100% + 10px);
}
.share-popover-title { font-size: 0.65rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1.5px; color: var(--muted2); margin-bottom: 10px; }
.share-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.share-option {
  display: flex; align-items: center; gap: 8px;
  background: var(--surface3); border: 1px solid var(--border); border-radius: 9px;
  padding: 8px 11px; cursor: pointer; text-decoration: none;
  font-size: 0.78rem; font-weight: 600; color: var(--muted);
  transition: border-color 0.18s, color 0.18s, background 0.18s;
  font-family: inherit; white-space: nowrap;
}
.share-option:hover { background: var(--surface); }
.share-option.so-x:hover       { border-color: #e7e7e7; color: #e7e7e7; }
.share-option.so-whatsapp:hover { border-color: #25d366; color: #25d366; }
.share-option.so-telegram:hover { border-color: #2aabee; color: #2aabee; }
.share-option.so-linkedin:hover { border-color: #0a66c2; color: #0a66c2; }
.share-option.so-copy:hover     { border-color: var(--cyber); color: var(--cyber); }
.share-option.so-copy.copied   { border-color: var(--cyber); color: var(--cyber); }
.share-option-icon { font-size: 1rem; flex-shrink: 0; }
.share-copy-full {
  grid-column: 1 / -1; justify-content: center;
  border-color: var(--border2); color: var(--muted2);
}
.share-divider { grid-column: 1 / -1; height: 1px; background: var(--border); margin: 2px 0; }

/* Story footer */
.story-footer { padding: 11px 22px; border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; background: rgba(255,255,255,0.015); }
.src-link { color: var(--ai); text-decoration: none; font-size: 0.82rem; font-weight: 600; display: flex; align-items: center; gap: 5px; transition: color 0.15s; }
.src-link:hover { color: var(--purple); }
.src-name { color: var(--muted2); font-size: 0.76rem; }

/* ── Notables Grid ── */
.notable-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 12px;
}

.notable-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 18px 20px;
  cursor: pointer;
  transition: border-color 0.25s, box-shadow 0.25s;
  user-select: none;
}
.notable-card:hover { border-color: var(--border2); box-shadow: 0 6px 24px rgba(0,0,0,0.3); }
.notable-card.open { border-color: rgba(251,191,36,0.35); box-shadow: 0 6px 24px var(--glow-notables); }
.notable-card.kbd-focus { border-color: var(--notables) !important; box-shadow: 0 0 0 2px rgba(251,191,36,0.2) !important; }

.notable-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.notable-meta { display: flex; align-items: center; gap: 8px; }
.notable-cat {
  font-size: 0.62rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; padding: 2px 9px; border-radius: 20px;
}
.notable-src { font-size: 0.72rem; color: var(--muted2); font-weight: 500; }
.notable-chevron {
  font-size: 0.65rem; color: var(--muted2);
  transition: transform 0.3s cubic-bezier(0.4,0,0.2,1), color 0.2s;
}
.notable-card.open .notable-chevron { transform: rotate(180deg); color: var(--notables); }

.notable-headline { font-size: 0.97rem; font-weight: 700; line-height: 1.4; margin-bottom: 9px; }
.notable-summary { font-size: 0.86rem; color: var(--muted); line-height: 1.65; }

.notable-body {
  max-height: 0; overflow: hidden; opacity: 0;
  transition: max-height 0.4s cubic-bezier(0.4,0,0.2,1), opacity 0.3s ease;
}
.notable-card.open .notable-body { max-height: 600px; opacity: 1; }

.notable-apply {
  border-top: 1px solid var(--border);
  margin-top: 14px; padding-top: 14px;
}
.notable-apply .blabel { color: var(--notables2); margin-bottom: 8px; }
.notable-apply p { font-size: 0.88rem; color: var(--body-text); line-height: 1.7; }
.notable-read {
  display: inline-block; margin-top: 12px;
  color: var(--notables); text-decoration: none;
  font-size: 0.8rem; font-weight: 600;
  transition: color 0.15s;
}
.notable-read:hover { color: var(--notables2); }

/* ── Keyboard hint ── */
#kbd-hint {
  position: fixed; bottom: 24px; right: 20px;
  background: var(--surface2); border: 1px solid var(--border2);
  border-radius: 10px; padding: 10px 14px;
  font-size: 0.72rem; color: var(--muted); line-height: 1.9;
  opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 200;
}
#kbd-hint.visible { opacity: 1; }
kbd {
  display: inline-block; background: var(--surface3); border: 1px solid var(--border2);
  border-radius: 4px; padding: 1px 5px; font-family: monospace; font-size: 0.7rem; color: var(--text);
}

/* ── Tech Tags ── */
.tag-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.tag {
  font-size: 0.62rem; font-weight: 600; font-family: 'Courier New', monospace;
  padding: 3px 9px; border-radius: 5px;
  background: var(--surface3); color: var(--muted);
  border: 1px solid var(--border2);
  cursor: pointer; line-height: 1.4;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.tag:hover { color: var(--text); border-color: var(--muted2); background: var(--surface3); }
.tag-cve { background: rgba(239,68,68,0.08); color: #f87171; border-color: rgba(239,68,68,0.25); }
.tag-cve:hover { background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.5); }

/* ── Tag Modal ── */
.tag-modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.65);
  display: flex; align-items: center; justify-content: center;
  z-index: 2000;
  opacity: 0; pointer-events: none;
  transition: opacity 0.2s;
  backdrop-filter: blur(6px);
  padding: 16px;
}
.tag-modal-overlay.open { opacity: 1; pointer-events: all; }
.tag-modal-box {
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 16px;
  padding: 28px 28px 24px;
  max-width: 460px; width: 100%;
  position: relative;
  transform: scale(0.93) translateY(10px);
  transition: transform 0.22s cubic-bezier(0.4,0,0.2,1);
  box-shadow: 0 32px 80px rgba(0,0,0,0.55);
}
.tag-modal-overlay.open .tag-modal-box { transform: scale(1) translateY(0); }
.tag-modal-close {
  position: absolute; top: 14px; right: 14px;
  width: 30px; height: 30px; border-radius: 50%;
  background: var(--surface3); border: 1px solid var(--border2);
  color: var(--muted); font-size: 0.9rem; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s;
}
.tag-modal-close:hover { background: var(--border2); color: var(--text); }
#tag-modal-name {
  font-size: 1.1rem; font-weight: 800;
  font-family: 'Courier New', monospace;
  padding-right: 36px; margin-bottom: 18px;
  color: var(--text);
}
.tag-modal-overlay.cve #tag-modal-name { color: #f87171; }
.tag-modal-section-label {
  font-size: 0.62rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1.5px; color: var(--muted2); margin-bottom: 6px;
}
#tag-modal-desc {
  font-size: 0.9rem; color: var(--body-text); line-height: 1.72; margin-bottom: 16px;
}
#tag-modal-relevance {
  font-size: 0.88rem; color: var(--muted); line-height: 1.72;
  padding: 12px 15px; border-radius: 8px;
  background: var(--surface3);
  border-left: 3px solid var(--ai);
  font-style: italic;
}
.tag-modal-overlay.cve #tag-modal-relevance { border-left-color: #f87171; }

/* ── Affected Systems ── */
.affected-block { background: rgba(239,68,68,0.04); border-top: 1px solid rgba(239,68,68,0.15) !important; }
.affected-header { display: flex; align-items: center; gap: 8px; margin-bottom: 13px; }
.affected-header .blabel { margin-bottom: 0; color: #f87171; }
.affected-warning { font-size: 0.65rem; color: #f87171; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.2); border-radius: 4px; padding: 1px 7px; font-weight: 700; }
.affected-list { display: flex; flex-direction: column; gap: 8px; }
.affected-item {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  background: var(--surface2); border: 1px solid rgba(239,68,68,0.18);
  border-radius: 9px; padding: 10px 14px;
}
.affected-name { font-weight: 700; font-size: 0.9rem; color: var(--text); }
.affected-ver {
  font-family: 'Courier New', monospace; font-size: 0.77rem;
  color: #fca5a5; background: rgba(239,68,68,0.1);
  padding: 3px 9px; border-radius: 5px; border: 1px solid rgba(239,68,68,0.2);
  white-space: normal; word-break: break-word;
}

/* Site footer */
.site-footer { text-align: center; padding: 40px 20px; color: var(--muted2); font-size: 0.8rem; border-top: 1px solid var(--border); }
.site-footer a { color: var(--ai); text-decoration: none; }

/* ── Card Maker ── */
.cm-wrap { padding: 32px 0; }
.cm-header { margin-bottom: 24px; }
.cm-title { font-size: 1.35rem; font-weight: 800; letter-spacing: -0.5px; color: var(--text); margin: 0 0 8px; }
.cm-subtitle { color: var(--muted); font-size: 0.88rem; line-height: 1.65; margin: 0; }
.cm-form { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 22px; margin-bottom: 18px; }
.cm-field { margin-bottom: 14px; }
.cm-field:last-of-type { margin-bottom: 0; }
.cm-label { display: block; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 6px; }
.cm-note { font-size: 0.68rem; font-weight: 400; text-transform: none; letter-spacing: 0; color: var(--muted2); margin-left: 8px; }
.cm-input { width: 100%; padding: 10px 13px; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 0.88rem; font-family: inherit; box-sizing: border-box; transition: border-color 0.2s, box-shadow 0.2s; outline: none; }
.cm-input:focus { border-color: #f472b6; box-shadow: 0 0 0 3px rgba(244,114,182,0.12); }
.cm-or { text-align: center; color: var(--muted2); font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; margin: 10px 0; position: relative; }
.cm-or::before, .cm-or::after { content: ''; position: absolute; top: 50%; width: 44%; height: 1px; background: var(--border); }
.cm-or::before { left: 0; } .cm-or::after { right: 0; }
.cm-textarea { width: 100%; padding: 10px 13px; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 0.85rem; font-family: inherit; resize: vertical; box-sizing: border-box; min-height: 90px; outline: none; transition: border-color 0.2s, box-shadow 0.2s; }
.cm-textarea:focus { border-color: #f472b6; box-shadow: 0 0 0 3px rgba(244,114,182,0.12); }
.cm-btn { width: 100%; padding: 13px; background: linear-gradient(135deg, #f472b6 0%, #e879f9 100%); color: #fff; font-weight: 700; font-size: 0.93rem; border: none; border-radius: 10px; cursor: pointer; margin-top: 16px; transition: opacity 0.2s, transform 0.1s; letter-spacing: 0.3px; }
.cm-btn:hover:not(:disabled) { opacity: 0.88; }
.cm-btn:active:not(:disabled) { transform: scale(0.985); }
.cm-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.cm-status { margin: 14px 0 4px; padding: 11px 15px; border-radius: 9px; font-size: 0.84rem; display: none; }
.cm-status.show { display: block; }
.cm-status.loading { background: rgba(251,191,36,0.08); border: 1px solid rgba(251,191,36,0.25); color: #fbbf24; }
.cm-status.error   { background: rgba(239,68,68,0.08);  border: 1px solid rgba(239,68,68,0.2);  color: #fca5a5; }
.cm-status.success { background: rgba(52,211,153,0.07); border: 1px solid rgba(52,211,153,0.2); color: #34d399; }
.cm-progress { display: flex; align-items: center; gap: 10px; }
.cm-spinner { width: 14px; height: 14px; border: 2px solid rgba(251,191,36,0.25); border-top-color: #fbbf24; border-radius: 50%; animation: spin 0.75s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
.cm-output-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.cm-output-label { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--muted2); }
.cm-actions { display: flex; gap: 7px; }
.cm-action-btn { font-size: 0.75rem; font-weight: 600; padding: 5px 13px; border-radius: 20px; border: 1px solid var(--border); background: var(--surface2); color: var(--muted); cursor: pointer; transition: border-color 0.18s, color 0.18s; }
.cm-action-btn:hover { border-color: #f472b6; color: #f472b6; }
.cm-maker-badge { font-size: 0.58rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; padding: 2px 7px; border-radius: 10px; background: rgba(244,114,182,0.15); color: #f472b6; border: 1px solid rgba(244,114,182,0.3); vertical-align: middle; margin-left: 5px; }
.cm-key-hint { font-size: 0.73rem; color: var(--muted2); margin-top: 8px; text-align: center; }
.cm-key-hint a { color: #f472b6; text-decoration: none; }
.cm-key-hint a:hover { text-decoration: underline; }

/* Mobile */
@media (max-width: 640px) {
  .site-header h1 { font-size: 2rem; letter-spacing: -1px; }
  .story-summary { padding: 16px; gap: 10px; }
  .story-summary h2 { font-size: 0.97rem; }
  .block { padding: 16px; }
  pre.ascii { font-size: 0.6rem; padding: 10px; }
  .insights-grid { grid-template-columns: 1fr; }
  .notable-grid { grid-template-columns: 1fr; }
  .tab-btn { font-size: 0.78rem; padding: 13px 8px; gap: 5px; }
  #kbd-hint { display: none; }
}
</style>
</head>
<body>

<div id="progress-bar"></div>

<div id="tag-modal" class="tag-modal-overlay" onclick="closeTagModal(event)">
  <div class="tag-modal-box" onclick="event.stopPropagation()">
    <button class="tag-modal-close" onclick="closeTagModal(event)">&#x2715;</button>
    <div id="tag-modal-name"></div>
    <div class="tag-modal-section-label">What it is</div>
    <div id="tag-modal-desc"></div>
    <div class="tag-modal-section-label">In this story</div>
    <div id="tag-modal-relevance"></div>
  </div>
</div>

<header class="site-header">
  <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn">☀️ Light</button>
  <div class="eyebrow">Your daily briefing</div>
  <h1>The Daily Rundown</h1>
  <p class="header-tagline">AI, cybersecurity, and the stories that actually matter — digested by Claude so you don't have to doom-scroll for them.</p>
  <div class="date-badge"><span class="date-dot"></span>__DATE__</div>
  <a href="guide.html" class="guide-link">&#x1F5FA; How to read this digest &#x2192;</a>
</header>

<div class="tabs-wrap">
  <div class="tabs">
    <button class="tab-btn active" id="tab-ai" onclick="switchTab('ai',this)">&#x1F916; AI &amp; Technology</button>
    <button class="tab-btn" id="tab-cyber" onclick="switchTab('cyber',this)">&#x1F510; Cybersecurity</button>
    <button class="tab-btn" id="tab-notables" onclick="switchTab('notables',this)">&#x1F4F0; Notables</button>
    <button class="tab-btn" id="tab-cardmaker" onclick="switchTab('cardmaker',this)">&#x270F;&#xFE0F; Card Maker</button>
    <div class="tab-indicator" id="indicator"></div>
  </div>
</div>

<main class="content">
  <section id="ai" class="section active">
    <div class="section-bar">
      <span class="section-label">3 stories &mdash; tap to expand</span>
      <button class="expand-all" onclick="expandAll('ai', this)">Expand all</button>
    </div>
    __AI_STORIES__
  </section>
  <section id="cyber" class="section">
    <div class="section-bar">
      <span class="section-label">3 stories &mdash; tap to expand</span>
      <button class="expand-all" onclick="expandAll('cyber', this)">Expand all</button>
    </div>
    __CYBER_STORIES__
  </section>
  <section id="notables" class="section">
    <div class="section-bar">
      <span class="section-label">5 highlights &mdash; tap to expand</span>
    </div>
    <div class="notable-grid">
      __NOTABLES__
    </div>
  </section>

  <section id="cardmaker" class="section">
    <div class="cm-wrap">
      <div class="cm-header">
        <h2 class="cm-title">&#x270F;&#xFE0F; Rundown Card Maker</h2>
        <p class="cm-subtitle">Paste any news article URL and Claude will analyze it in real time and generate a full story card &mdash; same format, same depth as the daily digest.</p>
      </div>
      <div class="cm-form" id="cm-form">
        <div class="cm-field">
          <label class="cm-label" for="cm-url">Article URL</label>
          <input class="cm-input" id="cm-url" type="url" placeholder="https://...">
        </div>
        <div class="cm-or">or</div>
        <div class="cm-field">
          <label class="cm-label" for="cm-text">Paste Article Text <span class="cm-note">include the headline</span></label>
          <textarea class="cm-textarea" id="cm-text" rows="5" placeholder="Paste the article headline and body text here..."></textarea>
        </div>
        <button class="cm-btn" id="cm-btn" onclick="cmGenerate()">&#x2728;&nbsp; Generate Card</button>
      </div>
      <div class="cm-status" id="cm-status"></div>
      <div id="cm-output"></div>
    </div>
  </section>
</main>

<footer class="site-footer">
  The Daily Rundown &middot; Generated with Claude Opus &middot; <a href="https://github.com/dizchrisctrl/daily-digest">GitHub</a>
</footer>

<div id="kbd-hint">
  <kbd>j</kbd> / <kbd>k</kbd> navigate &nbsp; <kbd>Enter</kbd> expand<br>
  <kbd>1</kbd> AI &nbsp; <kbd>2</kbd> Cyber &nbsp; <kbd>3</kbd> Notables
</div>

<script>
const indicator = document.getElementById('indicator');
const tabColors = { ai: '#818cf8', cyber: '#34d399', notables: '#fbbf24', cardmaker: '#f472b6' };
let currentIndex = -1;
let kbdTimeout;

// ── Progress bar ──
window.addEventListener('scroll', () => {
  const doc = document.documentElement;
  const pct = doc.scrollHeight - doc.clientHeight > 0
    ? (doc.scrollTop / (doc.scrollHeight - doc.clientHeight)) * 100 : 0;
  document.getElementById('progress-bar').style.width = pct + '%';
}, { passive: true });

// ── Tab switching ──
function positionIndicator(btn) {
  indicator.style.left  = btn.offsetLeft + 'px';
  indicator.style.width = btn.offsetWidth + 'px';
}

function switchTab(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  indicator.style.background = tabColors[id] || '#818cf8';
  positionIndicator(btn);
  currentIndex = -1;
  clearKbdFocus();
}

window.addEventListener('load', () => {
  const active = document.querySelector('.tab-btn.active');
  if (active) positionIndicator(active);

  // ── Deep-link: open and highlight the story matching the URL hash ──
  const hash = window.location.hash.slice(1); // e.g. "story-ai-1"
  if (!hash) return;
  const card = document.getElementById(hash);
  if (!card) return;

  // Switch to the correct tab using stable IDs
  let tabId = null;
  if (hash.startsWith('story-ai-'))    tabId = 'ai';
  else if (hash.startsWith('story-cyber-')) tabId = 'cyber';
  if (tabId) {
    const btn = document.getElementById('tab-' + tabId);
    if (btn) switchTab(tabId, btn);
  }

  // Expand card immediately, then after two paint frames (ensuring display:block
  // has been fully laid out) scroll to it and fire the highlight animation.
  card.classList.add('open');
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      // Small extra delay so the scroll starts before the glow draws attention
      setTimeout(() => {
        card.classList.add('story-highlight');
        card.addEventListener('animationend', () => card.classList.remove('story-highlight'), { once: true });
      }, 300);
    });
  });
});

// ── Tag modal ──
function openTagModal(e, btn) {
  e.stopPropagation();
  const overlay = document.getElementById('tag-modal');
  const name = btn.dataset.name || '';
  document.getElementById('tag-modal-name').textContent = name;
  document.getElementById('tag-modal-desc').textContent = btn.dataset.desc || '';
  document.getElementById('tag-modal-relevance').textContent = btn.dataset.relevance || '';
  overlay.classList.toggle('cve', name.toUpperCase().startsWith('CVE-'));
  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeTagModal(e) {
  if (e) e.stopPropagation();
  document.getElementById('tag-modal').classList.remove('open');
  document.body.style.overflow = '';
}

// ── Story card toggle ──
function toggleStory(card) { card.classList.toggle('open'); }
function toggleCard(card) { card.classList.toggle('open'); }
function toggleCollapse(head) { head.closest('.collapsible').classList.toggle('open'); }
function toggleNotable(card) { card.classList.toggle('open'); }

// ── Expand all ──
function expandAll(sectionId, btn) {
  const cards = document.querySelectorAll('#' + sectionId + ' .story-card');
  const allOpen = [...cards].every(c => c.classList.contains('open'));
  cards.forEach(c => c.classList.toggle('open', !allOpen));
  btn.textContent = allOpen ? 'Expand all' : 'Collapse all';
}

// ── Keyboard navigation ──
function getActiveCards() {
  const section = document.querySelector('.section.active');
  return section ? [...section.querySelectorAll('.story-card, .notable-card')] : [];
}

function clearKbdFocus() {
  document.querySelectorAll('.kbd-focus').forEach(el => el.classList.remove('kbd-focus'));
}

function focusCard(cards, idx) {
  clearKbdFocus();
  if (idx < 0 || idx >= cards.length) return;
  cards[idx].classList.add('kbd-focus');
  cards[idx].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function showKbdHint() {
  const hint = document.getElementById('kbd-hint');
  hint.classList.add('visible');
  clearTimeout(kbdTimeout);
  kbdTimeout = setTimeout(() => hint.classList.remove('visible'), 2500);
}

// ── Theme toggle ──
function applyTheme(light) {
  document.documentElement.classList.toggle('light', light);
  document.getElementById('theme-btn').textContent = light ? '🌙 Dark' : '☀️ Light';
}
function toggleTheme() {
  const isLight = !document.documentElement.classList.contains('light');
  localStorage.setItem('tdr-theme', isLight ? 'light' : 'dark');
  applyTheme(isLight);
}
(function() {
  const saved = localStorage.getItem('tdr-theme');
  const preferLight = saved ? saved === 'light' : window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(preferLight);
})();

// ── Share ──
(function() {
  let _openPopover = null;

  function _closeAll() {
    if (_openPopover) {
      _openPopover.classList.remove('open');
      _openPopover.closest('.share-wrap').querySelector('.share-btn').classList.remove('share-active');
      _openPopover = null;
    }
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('.share-wrap')) _closeAll();
  });

  window.toggleShare = function(e, btn) {
    e.stopPropagation();
    const wrap = btn.closest('.share-wrap');
    const pop  = wrap.querySelector('.share-popover');
    if (pop === _openPopover) { _closeAll(); return; }
    _closeAll();
    pop.classList.add('open');
    btn.classList.add('share-active');
    _openPopover = pop;
  };

  window.shareNative = function(e, btn) {
    e.stopPropagation();
    const card  = btn.closest('.story-card');
    const title = card.dataset.shareTitle || '';
    const text  = card.dataset.shareText  || '';
    const url   = card.dataset.shareUrl   || window.location.href;
    if (navigator.share) {
      navigator.share({ title, text, url }).catch(() => {});
    } else {
      const wrap = btn.closest('.share-wrap');
      const pop  = wrap.querySelector('.share-popover');
      if (pop === _openPopover) { _closeAll(); return; }
      _closeAll();
      pop.classList.add('open');
      btn.classList.add('share-active');
      _openPopover = pop;
    }
  };

  window.copyShareLink = function(e, btn) {
    e.stopPropagation();
    const card = btn.closest('.story-card');
    const url  = card.dataset.shareUrl || window.location.href;
    const text = card.dataset.shareTitle ? card.dataset.shareTitle + ' — ' + url : url;
    navigator.clipboard.writeText(text).then(() => {
      btn.classList.add('copied');
      const label = btn.querySelector('.share-copy-label');
      if (label) { label.textContent = 'Copied!'; setTimeout(() => { label.textContent = 'Copy link'; btn.classList.remove('copied'); }, 2000); }
    }).catch(() => {
      // fallback for browsers without clipboard API
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      const label = btn.querySelector('.share-copy-label');
      if (label) { label.textContent = 'Copied!'; setTimeout(() => { label.textContent = 'Copy link'; }, 2000); }
    });
  };
})();

// ── Audio / Text-to-Speech ──
(function() {
  if (!window.speechSynthesis) {
    document.querySelectorAll('.audio-row').forEach(r => r.style.display = 'none');
    return;
  }

  let _current = null; // { card, utterance }

  function _setBtn(card, state) {
    const btn    = card.querySelector('.audio-btn');
    const stop   = card.querySelector('.audio-stop');
    const status = card.querySelector('.audio-status');
    if (!btn) return;
    btn.classList.remove('au-playing', 'au-paused');
    if (state === 'playing') {
      btn.textContent = '⏸ Pause'; btn.classList.add('au-playing');
      btn.setAttribute('aria-label', 'Pause narration');
      stop.classList.add('au-visible'); status.classList.add('au-visible');
    } else if (state === 'paused') {
      btn.textContent = '▶ Resume'; btn.classList.add('au-paused');
      btn.setAttribute('aria-label', 'Resume narration');
      stop.classList.add('au-visible'); status.classList.remove('au-visible');
    } else {
      btn.textContent = '🔊 Listen'; btn.removeAttribute('aria-pressed');
      btn.setAttribute('aria-label', 'Listen to this story');
      stop.classList.remove('au-visible'); status.classList.remove('au-visible');
    }
  }

  window.toggleAudio = function(card) {
    const synth = window.speechSynthesis;
    if (_current && _current.card === card) {
      if (synth.paused) { synth.resume(); _setBtn(card, 'playing'); }
      else              { synth.pause();  _setBtn(card, 'paused');  }
      return;
    }
    if (_current) { synth.cancel(); _setBtn(_current.card, 'idle'); _current = null; }
    const text = card.dataset.tts;
    if (!text) return;
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = 0.93; utt.pitch = 1.0;
    utt.onend = utt.onerror = () => { _setBtn(card, 'idle'); _current = null; };
    _current = { card, utterance: utt };
    _setBtn(card, 'playing');
    synth.speak(utt);
  };

  window.stopAudio = function(e, card) {
    e.stopPropagation();
    window.speechSynthesis.cancel();
    if (_current) { _setBtn(_current.card, 'idle'); _current = null; }
  };
})();

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeTagModal(); return; }
  if (e.target.matches('input, textarea, select')) return;
  if (document.getElementById('tag-modal').classList.contains('open')) return;

  const cards = getActiveCards();

  if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    currentIndex = Math.min(currentIndex + 1, cards.length - 1);
    focusCard(cards, currentIndex);
    showKbdHint();
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    currentIndex = Math.max(currentIndex - 1, 0);
    focusCard(cards, currentIndex);
    showKbdHint();
  } else if ((e.key === 'Enter' || e.key === ' ') && currentIndex >= 0 && cards[currentIndex]) {
    e.preventDefault();
    cards[currentIndex].classList.toggle('open');
  } else if (e.key === '1') {
    switchTab('ai', document.querySelector('[onclick*="\'ai\'"]'));
    showKbdHint();
  } else if (e.key === '2') {
    switchTab('cyber', document.querySelector('[onclick*="\'cyber\'"]'));
    showKbdHint();
  } else if (e.key === '3') {
    switchTab('notables', document.querySelector('[onclick*="\'notables\'"]'));
    showKbdHint();
  }
});

// ── Card Maker ──────────────────────────────────────────────────────────────
(function() {

const CM_WORKER_URL = '__WORKER_URL__';

// Disable form + show notice if the Worker URL hasn't been configured yet
window.addEventListener('DOMContentLoaded', () => {
  if (!CM_WORKER_URL) {
    const btn = document.getElementById('cm-btn');
    if (btn) btn.disabled = true;
    const form = document.getElementById('cm-form');
    if (form) {
      const notice = document.createElement('p');
      notice.className = 'cm-key-hint';
      notice.style.cssText = 'color:#fca5a5;margin-top:12px;text-align:center';
      notice.textContent = 'Card Maker is not yet configured — deploy the Cloudflare Worker and add WORKER_URL to your repo secrets.';
      form.appendChild(notice);
    }
  }
});

// ── Status helpers ──
function cmStatus(type, html) {
  const el = document.getElementById('cm-status');
  el.className = 'cm-status show ' + type;
  el.innerHTML = type === 'loading'
    ? `<div class="cm-progress"><div class="cm-spinner"></div><span>${html}</span></div>`
    : html;
}
function cmHideStatus() { document.getElementById('cm-status').className = 'cm-status'; }

// ── SVG sanitiser (matches Python sanitize_svg — deny dangerous elements/attrs) ──
function cmSanitizeSvg(s) {
  return s
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<foreignObject[\s\S]*?<\/foreignObject>/gi, '')
    .replace(/<image[^>]*>/gi, '')
    .replace(/\s+on\w+\s*=\s*["'][^"']*["']/gi, '')
    .replace(/href\s*=\s*["']javascript:[^"']*["']/gi, '')
    .replace(/xlink:href\s*=\s*["']javascript:[^"']*["']/gi, '');
}

// ── HTML escaper ──
function h(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// ── Preview truncator (for collapsible section teasers) ──
function cmPreview(s, n) {
  n = n || 90;
  s = (s || '').trim();
  return s.length > n ? s.slice(0, n) + '\u2026' : s;
}
// ── Estimated read time at 200 wpm (technical content) ──
function cmReadTime(story) {
  const parts = [
    story.tldr, story.why_it_matters, story.concept_explained,
    story.opinion_assessment, story.devils_advocate,
    story.deep_dive, story.deep_dive_impact, story.deep_dive_outlook,
  ];
  (story.public_opinion||[]).forEach(o => parts.push(o.sentiment||''));
  (story.quiz||[]).forEach(q => { parts.push(q.q||'', q.a||'', q.explain||''); });
  const words = parts.filter(Boolean).join(' ').trim().split(/\s+/).length;
  return Math.max(1, Math.round(words / 200)) + ' min read';
}

// ── Tags HTML ──
function cmTagsHtml(tags) {
  if (!tags||!tags.length) return '';
  return '<div class="tags">' + tags.map(t =>
    `<button class="tag" onclick="openTagModal(event,this)" data-name="${h(t.name)}" data-desc="${h(t.description)}" data-relevance="${h(t.relevance)}">${h(t.name)}</button>`
  ).join('') + '</div>';
}

// ── Affected systems HTML ──
function cmAffectedHtml(sys) {
  if (!sys||!sys.length) return '';
  return '<div class="affected-block">' + sys.map(s =>
    `<div class="affected-row"><span class="affected-name">${h(s.name)}</span><span class="affected-ver">${h(s.versions)}</span></div>`
  ).join('') + '</div>';
}

// ── Render a full story card from Claude's structured output ──
const LENS_ICO = { Scientific:'&#x1F52C;', Historical:'&#x1F4DC;', Societal:'&#x1F30D;' };

function cmRenderCard(story, color) {
  color = color || '#f472b6';
  const headline   = story.headline   || '';
  const tldr       = story.tldr       || '';
  const source     = story.source     || 'Card Maker';
  const sourceUrl  = story.source_url || '#';
  const pubDate    = story.pub_date    || '';
  const anchor     = 'cm-card-' + Date.now();

  // Concept paragraphs
  const cParas = (story.concept_explained||'').split(/\n\n+/).filter(p=>p.trim())
    .map(p=>`<p>${h(p.trim())}</p>`).join('');
  const conceptFirstPara = ((story.concept_explained||'').split(/\n\n+/)[0] || '').trim();

  // Insights
  const quizHtml = (story.quiz||[]).map((q,i) => {
    const lens = q.lens||'';
    const ico  = LENS_ICO[lens]||'&#x1F4A1;';
    const cls  = lens ? `q-lens-${lens.toLowerCase()}` : '';
    return `<div class="qcard" onclick="toggleCard(this)">
      <div class="q-num">${ico} Insight ${i+1}${lens?` <span class="q-lens ${cls}">${h(lens)}</span>`:''}</div>
      <div class="q-text">${h(q.q||'')}</div>
      <div class="q-answer"><div class="q-divider"></div><div class="q-ans">${h(q.a||'')}</div><div class="q-exp">${h(q.explain||'')}</div></div>
      <div class="q-hint">&#9656; go deeper</div>
    </div>`;
  }).join('');

  // Public opinion entries
  const opinionHtml = (story.public_opinion||[]).map(o =>
    `<details class="opinion-entry"><summary><span class="opinion-chevron">&#9656;</span><span class="opinion-source">${h(o.source||'')}</span><span class="opinion-preview">${h(o.sentiment||'')}</span></summary><div class="opinion-full">${h(o.sentiment||'')}</div></details>`
  ).join('');

  // SVG diagram
  const svgHtml = story.visual_svg
    ? `<div class="block collapsible"><div class="collapsible-head" onclick="toggleCollapse(this)"><div class="blabel">&#x1F4CA; Visual Diagram</div><span class="collapsible-preview">${h(story.concept_title||'diagram')} &mdash; see how it works &#x2192;</span><span class="collapsible-chevron">&#9656;</span></div><div class="collapsible-body"><div class="diagram-wrap"><div class="diagram-bar"><span class="dot dot-r"></span><span class="dot dot-y"></span><span class="dot dot-g"></span><span class="diagram-title">${h(story.concept_title||'diagram')}</span></div><div class="diagram-svg">${cmSanitizeSvg(story.visual_svg)}</div></div></div></div>`
    : '';

  // TTS text (mirrors _build_tts_text logic)
  const ttsParts = [
    headline, 'Summary.', tldr,
    'Why it matters.', story.why_it_matters||'',
    'Concept:', story.concept_title||'', story.concept_explained||'',
    ...(story.public_opinion||[]).map(o=>`${o.source}: ${o.sentiment}`),
    'Sentiment summary.', story.opinion_assessment||'',
    "Devil's Advocate.", story.devils_advocate||'',
    ...(story.quiz||[]).map((q,i)=>`Insight ${i+1}, ${q.lens} perspective. ${q.q} ${q.a} ${q.explain}`),
    'Deep dive.', story.deep_dive||'',
    'How this affects you.', story.deep_dive_impact||'',
    'Outlook.', story.deep_dive_outlook||''
  ].filter(Boolean).join(' ');

  // Share URLs use current page (no per-card redirect page for maker cards)
  const shareUrl   = h(window.location.href);
  const shareTitle = h(headline);
  const shareText  = h(tldr);
  const twText = encodeURIComponent(('\uD83D\uDCF0 ' + headline + '\n\n' + tldr).slice(0,230));
  const waText = encodeURIComponent('\uD83D\uDCF0 *' + headline + '*\n\n' + tldr);
  const tgText = encodeURIComponent('\uD83D\uDCF0 ' + headline + '\n\n' + tldr);
  const eu     = encodeURIComponent(window.location.href);

  return `
<article class="story-card open" id="${anchor}" style="--accent:${color}" data-tts="${h(ttsParts)}" data-share-title="${shareTitle}" data-share-text="${shareText}" data-share-url="${shareUrl}">
  <div class="story-summary" onclick="toggleStory(this.closest(\'.story-card\'))">
    <div class="s-left">
      <div class="s-meta">
        <span class="src-badge" style="background:${color}1a;color:${color}">${h(source)}</span>
        <span class="read-time">&#x23F1; ${h(cmReadTime(story))}</span>
        <span class="story-num"><span class="cm-maker-badge">Card Maker</span></span>
      </div>
      <h2>${h(headline)}</h2>
      ${pubDate?`<div class="pub-date">&#x1F551; ${h(pubDate)}</div>`:''}
      <div class="tldr"><span class="tldr-tag">TL;DR</span>${h(tldr)}</div>
      ${cmTagsHtml(story.tech_tags||[])}
      <div class="audio-row" onclick="event.stopPropagation()">
        <button class="audio-btn" onclick="toggleAudio(this.closest(\'.story-card\'))" aria-label="Listen">&#x1F50A; Listen</button>
        <button class="audio-stop" onclick="stopAudio(event,this.closest(\'.story-card\'))" aria-label="Stop">&#x25A0; Stop</button>
        <span class="audio-status"><span class="audio-dot"></span>Listening&hellip;</span>
      </div>
    </div>
    <div class="chevron">&#9660;</div>
  </div>
  <div class="story-body"><div class="body-inner">
    ${cmAffectedHtml(story.affected_systems||[])}
    <div class="block"><div class="blabel">&#x1F4CC; Why It Matters</div><p>${h(story.why_it_matters||'')}</p></div>
    <div class="block concept-block collapsible">
      <div class="collapsible-head" onclick="toggleCollapse(this)">
        <div class="blabel">&#x1F9E0; Concept</div>
        <span class="collapsible-preview">${h(story.concept_title||'')} &mdash; ${h(cmPreview(conceptFirstPara))}</span>
        <span class="collapsible-chevron">&#9656;</span>
      </div>
      <div class="collapsible-body">
        <div class="concept-title" style="color:${color}">${h(story.concept_title||'')}</div>
        <div class="concept-text">${cParas}</div>
      </div>
    </div>
    ${svgHtml}
    <div class="block opinion-block">
      <div class="blabel">&#x1F465; Public Opinion</div>
      ${opinionHtml}
      <div class="blabel" style="margin-top:14px">&#x1F4CA; Sentiment Summary</div>
      <p>${h(story.opinion_assessment||'')}</p>
      <div class="block devil-block collapsible" style="margin-top:14px;padding:14px 16px;border-radius:8px">
        <div class="collapsible-head" onclick="toggleCollapse(this)">
          <div class="devil-intro">&#x1F608; Devil&#x2019;s Advocate</div>
          <span class="collapsible-preview">${h(cmPreview(story.devils_advocate||''))}</span>
          <span class="collapsible-chevron">&#9656;</span>
        </div>
        <div class="collapsible-body">
          <p class="devil-text">${h(story.devils_advocate||'')}</p>
        </div>
      </div>
    </div>
    <div class="block"><div class="blabel">&#x1F4A1; Insights</div><div class="insights-grid">${quizHtml}</div></div>
    <div class="block deepdive-block">
      <div class="blabel">&#x1F4AD; Deep Dive</div>
      <p class="deepdive-text">${h(story.deep_dive||'')}</p>
      ${story.deep_dive_impact?`<div class="deepdive-impact collapsible"><div class="collapsible-head" onclick="toggleCollapse(this)"><div class="deepdive-impact-label">&#x1F3AF; How This Affects You</div><span class="collapsible-preview">${h(cmPreview(story.deep_dive_impact))}</span><span class="collapsible-chevron">&#9656;</span></div><div class="collapsible-body"><p class="deepdive-impact-text">${h(story.deep_dive_impact)}</p></div></div>`:''}
      ${story.deep_dive_outlook?`<div class="deepdive-outlook collapsible"><div class="collapsible-head" onclick="toggleCollapse(this)"><div class="deepdive-outlook-label">&#x1F52D; Outlook</div><span class="collapsible-preview">${h(cmPreview(story.deep_dive_outlook))}</span><span class="collapsible-chevron">&#9656;</span></div><div class="collapsible-body"><p class="deepdive-outlook-text">${h(story.deep_dive_outlook)}</p></div></div>`:''}
    </div>
    <div class="story-footer">
      <a class="src-link" href="${h(sourceUrl)}" target="_blank" rel="noopener noreferrer">Read original <span>&#x2192;</span></a>
      <div class="share-wrap" onclick="event.stopPropagation()">
        <button class="share-btn" onclick="shareNative(event,this)" aria-label="Share">&#x1F517; Share</button>
        <div class="share-popover share-popover-up" onclick="event.stopPropagation()">
          <div class="share-popover-title">Share this story</div>
          <div class="share-grid">
            <a class="share-option so-x" href="https://twitter.com/intent/tweet?text=${twText}&url=${eu}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1D54F;</span>X / Twitter</a>
            <a class="share-option so-whatsapp" href="https://wa.me/?text=${waText}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1F4AC;</span>WhatsApp</a>
            <a class="share-option so-telegram" href="https://t.me/share/url?url=${eu}&text=${tgText}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x2708;</span>Telegram</a>
            <a class="share-option so-linkedin" href="https://www.linkedin.com/sharing/share-offsite/?url=${eu}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1F4BC;</span>LinkedIn</a>
            <div class="share-divider"></div>
            <button class="share-option so-copy share-copy-full" onclick="copyShareLink(event,this.closest(\'.story-card\'))"><span class="share-option-icon">&#x1F517;</span><span class="share-copy-label">Copy link</span></button>
          </div>
        </div>
      </div>
    </div>
  </div></div>
</article>`;
}

// ── Tool schema passed to Claude ──
const CM_TOOL = {
  name: 'publish_story',
  description: 'Publish one fully formatted digest story',
  input_schema: {
    type: 'object',
    required: ['headline','pub_date','tldr','why_it_matters','concept_title','concept_explained',
               'visual_svg','public_opinion','opinion_assessment','devils_advocate','quiz',
               'deep_dive','deep_dive_impact','deep_dive_outlook','source_url','source',
               'tech_tags','affected_systems'],
    properties: {
      headline:          { type:'string' },
      pub_date:          { type:'string' },
      tldr:              { type:'string' },
      why_it_matters:    { type:'string' },
      concept_title:     { type:'string' },
      concept_explained: { type:'string', description:'4 paragraphs separated by blank lines. P1: simple analogy. P2: technical mechanics. P3: tie to this story. P4: broader implications.' },
      visual_svg: {
        type:'string',
        description:'A complete <svg> element viewBox="0 0 700 340". No scripts or event handlers. Dark bg #060912, node fill #12152a, accent #f472b6. Show directional flow with arrowheads. 8-15 elements. Short precise labels.'
      },
      public_opinion: {
        type:'array',
        items: { type:'object', required:['source','sentiment'], properties:{ source:{type:'string'}, sentiment:{type:'string'} } }
      },
      opinion_assessment: { type:'string' },
      devils_advocate:    { type:'string' },
      quiz: {
        type:'array', minItems:3, maxItems:3,
        items: {
          type:'object', required:['lens','q','a','explain'],
          properties: {
            lens:    { type:'string', enum:['Scientific','Historical','Societal'] },
            q:       { type:'string' },
            a:       { type:'string' },
            explain: { type:'string' }
          }
        }
      },
      deep_dive:         { type:'string' },
      deep_dive_impact:  { type:'string' },
      deep_dive_outlook: { type:'string' },
      source_url:        { type:'string' },
      source:            { type:'string' },
      tech_tags: {
        type:'array', maxItems:3,
        items: { type:'object', required:['name','description','relevance'], properties:{ name:{type:'string'}, description:{type:'string'}, relevance:{type:'string'} } }
      },
      affected_systems: {
        type:'array',
        items: { type:'object', required:['name','versions'], properties:{ name:{type:'string'}, versions:{type:'string'} } }
      }
    }
  }
};

// ── Fetch article via Worker (server-side fetch avoids CORS/CSP issues) ──
async function cmFetchArticle(url) {
  const res = await fetch(CM_WORKER_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fetch_url: url }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Could not fetch article (HTTP ${res.status}). Try pasting the text instead.`);
  }
  const data = await res.json();
  if (data.error) throw new Error(data.error + ' — try pasting the article text instead.');
  return cmExtractText(data.html, url);
}

function cmExtractText(html, url) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll('script,style,nav,footer,header,aside,noscript,[class*="sidebar"],[class*="related"],[class*="newsletter"],[class*="cookie"],[class*="banner"],[id*="cookie"]')
    .forEach(el => el.remove());
  const title = doc.querySelector('h1')?.textContent?.trim() || doc.title || '';
  const main  = doc.querySelector('article,[role="main"],main,[class*="article-body"],[class*="post-content"],[class*="story-body"]') || doc.body;
  const raw   = (main.textContent || '').replace(/[ \t]{2,}/g,' ').replace(/\n{3,}/g,'\n\n').trim();
  const body  = raw.length > 8000 ? raw.slice(0, 8000) + '…' : raw;
  return `Title: ${title}\nURL: ${url}\n\n${body}`;
}

// ── Call Anthropic via Worker proxy ──
async function cmCallClaude(articleText, sourceUrl) {
  const today = new Date().toISOString().slice(0,10);
  const prompt = `Today is ${today}. Write ONE digest story about the article below for someone moderately technical.

ARTICLE:
${articleText}

Guidelines:
- concept_explained: 4 paragraphs (blank line between each). P1: simple real-world analogy. P2: how it technically works. P3: how it connects to this exact story. P4: broader implications.
- visual_svg: SVG diagram viewBox="0 0 700 340". Background #060912. Node fill #12152a. Accent color #f472b6. Include arrowheads via <defs><marker>. 8-15 elements. Short precise labels. Show actual directional flow — not floating boxes.
- quiz: 3 insight cards — card 1 Scientific lens, card 2 Historical lens, card 3 Societal lens. Each: thought-provoking hook (q), crisp key insight (a), 2-3 sentence explanation (explain).
- public_opinion: one entry each for Hacker News, Reddit, and Security Twitter/X.
- devils_advocate: sharp counter-perspective that challenges the dominant sentiment — an overlooked irony or reframe.
- source_url: "${sourceUrl||''}"
- deep_dive: 3-4 sentence narrative that draws the reader in like the opening of great longform journalism.
- deep_dive_impact: 2-3 sentences directly addressing how this affects the reader's work or security posture.
- deep_dive_outlook: 2-3 sentences on what to watch for in coming weeks or months.
- tech_tags: 0-3 tags max, only specific products/CVEs. Empty array is fine.
- affected_systems: for vulnerability stories only. Empty array otherwise.

Call the publish_story tool.`;

  const res = await fetch(CM_WORKER_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: 'claude-opus-4-6',
      max_tokens: 8000,
      tools: [CM_TOOL],
      tool_choice: { type: 'tool', name: 'publish_story' },
      messages: [{ role: 'user', content: prompt }]
    })
  });

  if (!res.ok) {
    let msg = `API error ${res.status}`;
    try { const e = await res.json(); msg = e.error?.message || msg; } catch(_) {}
    if (res.status === 401) throw new Error('Invalid API key. Check your key at console.anthropic.com.');
    if (res.status === 429) throw new Error(msg);  // use Worker's weekly-limit message verbatim
    throw new Error(msg);
  }

  const data = await res.json();
  const block = data.content?.find(b => b.type === 'tool_use');
  if (!block?.input) throw new Error('Claude did not return story data. Please try again.');
  return block.input;
}

// ── Main generate handler ──
window.cmGenerate = async function() {
  const url  = (document.getElementById('cm-url')?.value  || '').trim();
  const text = (document.getElementById('cm-text')?.value || '').trim();

  if (!CM_WORKER_URL) { cmStatus('error', '&#x26A0; Card Maker is not configured yet.'); return; }
  if (!url && !text)  { cmStatus('error', '&#x26A0; Paste an article URL or the article text.'); return; }

  const btn = document.getElementById('cm-btn');
  btn.disabled = true;

  try {
    let articleContent = text;
    if (url && !text) {
      cmStatus('loading', 'Fetching article&hellip;');
      articleContent = await cmFetchArticle(url);
    }
    cmStatus('loading', 'Analyzing with Claude Opus&hellip; (this takes ~20s)');
    const story = await cmCallClaude(articleContent, url);

    cmStatus('success', '&#x2713; Card generated &mdash; scroll down to view it.');
    setTimeout(cmHideStatus, 4000);

    const cardHtml = cmRenderCard(story, '#f472b6');
    const out = document.getElementById('cm-output');
    out.innerHTML = `
      <div class="cm-output-header">
        <span class="cm-output-label">Generated Card</span>
        <div class="cm-actions">
          <button class="cm-action-btn" onclick="cmCopyHtml(event)">Copy HTML</button>
          <button class="cm-action-btn" onclick="cmClear()">&#x2715; Clear</button>
        </div>
      </div>
      <div id="cm-card-wrap">${cardHtml}</div>`;

    // Scroll to card, then fire highlight animation
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const card = out.querySelector('.story-card');
      if (!card) return;
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(() => {
        card.classList.add('story-highlight');
        card.addEventListener('animationend', () => card.classList.remove('story-highlight'), { once: true });
      }, 350);
    }));

  } catch(err) {
    cmStatus('error', '&#x26A0; ' + h(err.message || 'Something went wrong. Try again.'));
  } finally {
    btn.disabled = false;
  }
};

window.cmCopyHtml = function(e) {
  const wrap = document.getElementById('cm-card-wrap');
  if (!wrap) return;
  const html = wrap.innerHTML;
  navigator.clipboard.writeText(html).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = html; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
  });
  const btn = e.target;
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = orig, 2000);
};

window.cmClear = function() {
  document.getElementById('cm-output').innerHTML = '';
  document.getElementById('cm-url').value = '';
  document.getElementById('cm-text').value = '';
  cmHideStatus();
};

})(); // end Card Maker

</script>
</body>
</html>"""


# ── HTML helpers ───────────────────────────────────────────────────────────────
def esc(text):
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _preview(text, n=90):
    """Return first n chars of text with ellipsis if truncated (for collapsible previews)."""
    t = (text or "").strip()
    return (t[:n] + "\u2026") if len(t) > n else t


def _read_time(story, wpm=200):
    """Estimate read time in minutes at 200 wpm (technical content)."""
    parts = [
        story.get('tldr', ''),
        story.get('why_it_matters', ''),
        story.get('concept_explained', ''),
        story.get('opinion_assessment', ''),
        story.get('devils_advocate', ''),
        story.get('deep_dive', ''),
        story.get('deep_dive_impact', ''),
        story.get('deep_dive_outlook', ''),
    ]
    for o in (story.get('public_opinion') or []):
        parts.append(o.get('sentiment', ''))
    for q in (story.get('quiz') or []):
        parts.extend([q.get('q', ''), q.get('a', ''), q.get('explain', '')])
    words = sum(len(p.split()) for p in parts if p)
    mins = max(1, round(words / wpm))
    return f"{mins} min read"


def safe_url(url):
    u = str(url).strip()
    if u.lower().startswith(("http://", "https://")):
        return esc(u)
    return "#"


def build_tags_html(tags):
    if not tags:
        return ""
    pills = ""
    for t in tags:
        # Accept both old string format (rebuild compat) and new object format
        if isinstance(t, dict):
            name      = str(t.get("name", "")).strip()
            desc      = str(t.get("description", "")).strip()
            relevance = str(t.get("relevance", "")).strip()
        else:
            name, desc, relevance = str(t).strip(), "", ""
        if not name:
            continue
        cls = "tag tag-cve" if name.upper().startswith("CVE-") else "tag"
        pills += (
            f'<button class="{cls}" '
            f'data-name="{esc(name)}" '
            f'data-desc="{esc(desc)}" '
            f'data-relevance="{esc(relevance)}" '
            f'onclick="openTagModal(event,this)">'
            f'{esc(name)}</button>'
        )
    return f'<div class="tag-row">{pills}</div>' if pills else ""


def build_affected_html(systems):
    if not systems:
        return ""
    rows = ""
    for s in systems:
        rows += f"""
      <div class="affected-item">
        <span class="affected-name">{esc(s.get('name',''))}</span>
        <span class="affected-ver">{esc(s.get('versions',''))}</span>
      </div>"""
    return f"""
      <div class="block affected-block">
        <div class="affected-header">
          <div class="blabel">&#x26A0;&#xFE0F; Affected Systems</div>
          <span class="affected-warning">PATCH CHECK</span>
        </div>
        <div class="affected-list">{rows}
        </div>
      </div>"""


def _build_tts_text(story):
    """Build a clean, naturally readable narration script for a story."""
    parts = []
    if story.get('headline'):
        parts.append(story['headline'] + '.')
    if story.get('tldr'):
        parts.append('Summary. ' + story['tldr'])
    if story.get('why_it_matters'):
        parts.append('Why it matters. ' + story['why_it_matters'])
    if story.get('concept_title') and story.get('concept_explained'):
        concept_text = story['concept_explained'].replace('\n\n', ' ').replace('\n', ' ')
        parts.append('Concept: ' + story['concept_title'] + '. ' + concept_text)
    if story.get('opinion_assessment'):
        parts.append('Public sentiment. ' + story['opinion_assessment'])
    if story.get('devils_advocate'):
        parts.append("Devil's advocate. " + story['devils_advocate'])
    for i, q in enumerate(story.get('quiz', []), 1):
        lens = q.get('lens', '')
        label = f'Insight {i}' + (f', {lens} perspective' if lens else '') + '.'
        parts.append(f'{label} {q.get("q","")} {q.get("a","")} {q.get("explain","")}')
    if story.get('deep_dive'):
        parts.append('Deep dive. ' + story['deep_dive'])
    if story.get('deep_dive_impact'):
        parts.append('How this affects you. ' + story['deep_dive_impact'])
    if story.get('deep_dive_outlook'):
        parts.append('Outlook. ' + story['deep_dive_outlook'])
    return ' '.join(parts)


def _build_visual(story):
    """Return the visual block — SVG preferred, ASCII pre as fallback for old digests."""
    svg = story.get('visual_svg', '').strip()
    if svg:
        return f'<div class="diagram-svg">{sanitize_svg(svg)}</div>'
    ascii_art = story.get('visual_ascii', '').strip()
    if ascii_art:
        return f'<pre class="ascii">{esc(ascii_art)}</pre>'
    return ''


def build_story_html(story, color, num, story_id=""):
    LENS_ICONS = {"Scientific": "&#x1F52C;", "Historical": "&#x1F4DC;", "Societal": "&#x1F30D;"}
    quiz_html = ""
    for i, q in enumerate(story.get("quiz", []), 1):
        lens = q.get("lens", "")
        lens_cls = f"q-lens-{lens.lower()}" if lens else ""
        lens_icon = LENS_ICONS.get(lens, "&#x1F4A1;")
        quiz_html += f"""
      <div class="qcard" onclick="toggleCard(this)">
        <div class="q-num">{lens_icon} Insight {i}{f' <span class="q-lens {lens_cls}">{esc(lens)}</span>' if lens else ''}</div>
        <div class="q-text">{esc(q.get('q',''))}</div>
        <div class="q-answer">
          <div class="q-divider"></div>
          <div class="q-ans">{esc(q.get('a',''))}</div>
          <div class="q-exp">{esc(q.get('explain',''))}</div>
        </div>
        <div class="q-hint">&#9656; go deeper</div>
      </div>"""

    concept_paras = "".join(
        f"<p>{esc(p.strip())}</p>"
        for p in story.get("concept_explained", "").split("\n\n")
        if p.strip()
    )

    num_str       = f"{num:02d}"
    tags_html     = build_tags_html(story.get("tech_tags", []))
    affected_html = build_affected_html(story.get("affected_systems", []))

    tts_text    = esc(_build_tts_text(story))
    anchor      = story_id if story_id else f"story-{num}"
    headline    = story.get('headline', '')
    tldr        = story.get('tldr', '')
    # Per-story redirect page carries story-specific OG tags for rich social previews
    story_page  = f"{PAGES_URL}/s/{anchor}.html"
    share_title = esc(headline)
    share_text  = esc(tldr)
    share_url   = esc(story_page)

    # Twitter: 280 char limit; URL ~23 chars; keep body ≤ 230
    _tw_tldr = tldr if len(tldr) <= 180 else tldr[:177] + "\u2026"
    _tw_text = f"\U0001f4f0 {headline}\n\n{_tw_tldr}"[:230]
    # WhatsApp: supports *bold*, full TL;DR
    _wa_text = f"\U0001f4f0 *{headline}*\n\n{tldr}\n\n\U0001f517 {story_page}"
    # Telegram: clean headline + summary, link preview handles the rest
    _tg_text = f"\U0001f4f0 {headline}\n\n{tldr}"

    _eu  = urllib.parse.quote(story_page)
    _etw = urllib.parse.quote(_tw_text)
    _ewa = urllib.parse.quote(_wa_text)
    _etg = urllib.parse.quote(_tg_text)

    share_links = {
        "x":        f"https://twitter.com/intent/tweet?text={_etw}&url={_eu}",
        "whatsapp": f"https://wa.me/?text={_ewa}",
        "telegram": f"https://t.me/share/url?url={_eu}&text={_etg}",
        "linkedin": f"https://www.linkedin.com/sharing/share-offsite/?url={_eu}",
    }

    return f"""
<article class="story-card" id="{anchor}" style="--accent:{color}" data-tts="{tts_text}" data-share-title="{share_title}" data-share-text="{share_text}" data-share-url="{share_url}">
  <div class="story-summary" onclick="toggleStory(this.closest('.story-card'))">
    <div class="s-left">
      <div class="s-meta">
        <span class="src-badge" style="background:{color}1a;color:{color}">{esc(story.get('source',''))}</span>
        <span class="read-time">&#x23F1; {esc(_read_time(story))}</span>
        <span class="story-num">{num_str}</span>
      </div>
      <h2>{esc(story.get('headline',''))}</h2>
      {f'<div class="pub-date">&#x1F551; {esc(story.get("pub_date",""))}</div>' if story.get('pub_date') else ''}
      <div class="tldr"><span class="tldr-tag">TL;DR</span>{esc(story.get('tldr',''))}</div>
      {tags_html}
      <div class="audio-row" onclick="event.stopPropagation()">
        <button class="audio-btn" onclick="toggleAudio(this.closest('.story-card'))" aria-label="Listen to this story">&#x1F50A; Listen</button>
        <button class="audio-stop" onclick="stopAudio(event,this.closest('.story-card'))" aria-label="Stop narration">&#x25A0; Stop</button>
        <span class="audio-status"><span class="audio-dot"></span>Listening&hellip;</span>
      </div>
    </div>
    <div class="chevron">&#9660;</div>
  </div>

  <div class="story-body">
    <div class="body-inner">
      {affected_html}
      <div class="block">
        <div class="blabel">&#x1F4CC; Why It Matters</div>
        <p>{esc(story.get('why_it_matters',''))}</p>
      </div>

      <div class="block concept-block collapsible">
        <div class="collapsible-head" onclick="toggleCollapse(this)">
          <div class="blabel">&#x1F9E0; Concept</div>
          <span class="collapsible-preview">{esc(story.get('concept_title',''))} &mdash; {esc(_preview((story.get('concept_explained','').split(chr(10)+chr(10)) or [''])[0]))}</span>
          <span class="collapsible-chevron">&#9656;</span>
        </div>
        <div class="collapsible-body">
          <div class="concept-title" style="color:{color}">{esc(story.get('concept_title',''))}</div>
          <div class="concept-text">{concept_paras}</div>
        </div>
      </div>

      <div class="block collapsible">
        <div class="collapsible-head" onclick="toggleCollapse(this)">
          <div class="blabel">&#x1F4CA; Visual Diagram</div>
          <span class="collapsible-preview">{esc(story.get('concept_title','diagram'))} &mdash; see how it works &#x2192;</span>
          <span class="collapsible-chevron">&#9656;</span>
        </div>
        <div class="collapsible-body">
          <div class="diagram-wrap">
            <div class="diagram-bar">
              <span class="dot dot-r"></span><span class="dot dot-y"></span><span class="dot dot-g"></span>
              <span class="diagram-title">{esc(story.get('concept_title','diagram'))}</span>
            </div>
            {_build_visual(story)}
          </div>
        </div>
      </div>

      <div class="block opinion-block">
        <div class="blabel">&#x1F465; Public Opinion</div>
        {"".join(f'<details class="opinion-entry"><summary><span class="opinion-chevron">&#9656;</span><span class="opinion-source">{esc(o.get("source",""))}</span><span class="opinion-preview">{esc(o.get("sentiment",""))}</span></summary><div class="opinion-full">{esc(o.get("sentiment",""))}</div></details>' for o in (story.get("public_opinion") or []))}
        <div class="blabel" style="margin-top:14px">&#x1F4CA; Sentiment Summary</div>
        <p>{esc(story.get('opinion_assessment',''))}</p>
        <div class="block devil-block collapsible" style="margin-top:14px;padding:14px 16px;border-radius:8px">
          <div class="collapsible-head" onclick="toggleCollapse(this)">
            <div class="devil-intro">&#x1F608; Devil&#x2019;s Advocate</div>
            <span class="collapsible-preview">{esc(_preview(story.get('devils_advocate','')))}</span>
            <span class="collapsible-chevron">&#9656;</span>
          </div>
          <div class="collapsible-body">
            <p class="devil-text">{esc(story.get('devils_advocate',''))}</p>
          </div>
        </div>
      </div>

      <div class="block">
        <div class="blabel">&#x1F4A1; Insights</div>
        <div class="insights-grid">{quiz_html}</div>
      </div>

      <div class="block deepdive-block">
        <div class="blabel">&#x1F4AD; Deep Dive</div>
        <p class="deepdive-text">{esc(story.get('deep_dive',''))}</p>
        {f'<div class="deepdive-impact collapsible"><div class="collapsible-head" onclick="toggleCollapse(this)"><div class="deepdive-impact-label">&#x1F3AF; How This Affects You</div><span class="collapsible-preview">{esc(_preview(story.get("deep_dive_impact","")))}</span><span class="collapsible-chevron">&#9656;</span></div><div class="collapsible-body"><p class="deepdive-impact-text">{esc(story.get("deep_dive_impact",""))}</p></div></div>' if story.get('deep_dive_impact') else ''}
        {f'<div class="deepdive-outlook collapsible"><div class="collapsible-head" onclick="toggleCollapse(this)"><div class="deepdive-outlook-label">&#x1F52D; Outlook</div><span class="collapsible-preview">{esc(_preview(story.get("deep_dive_outlook","")))}</span><span class="collapsible-chevron">&#9656;</span></div><div class="collapsible-body"><p class="deepdive-outlook-text">{esc(story.get("deep_dive_outlook",""))}</p></div></div>' if story.get('deep_dive_outlook') else ''}
      </div>

      <div class="story-footer">
        <a class="src-link" href="{safe_url(story.get('source_url','#'))}" target="_blank" rel="noopener noreferrer">
          Read original <span>&#x2192;</span>
        </a>
        <div class="share-wrap" onclick="event.stopPropagation()">
          <button class="share-btn" onclick="shareNative(event,this)" aria-label="Share this story">&#x1F517; Share</button>
          <div class="share-popover share-popover-up" onclick="event.stopPropagation()">
            <div class="share-popover-title">Share this story</div>
            <div class="share-grid">
              <a class="share-option so-x" href="{esc(share_links['x'])}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1D54F;</span>X / Twitter</a>
              <a class="share-option so-whatsapp" href="{esc(share_links['whatsapp'])}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1F4AC;</span>WhatsApp</a>
              <a class="share-option so-telegram" href="{esc(share_links['telegram'])}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x2708;</span>Telegram</a>
              <a class="share-option so-linkedin" href="{esc(share_links['linkedin'])}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><span class="share-option-icon">&#x1F4BC;</span>LinkedIn</a>
              <div class="share-divider"></div>
              <button class="share-option so-copy share-copy-full" onclick="copyShareLink(event,this.closest('.story-card'))"><span class="share-option-icon">&#x1F517;</span><span class="share-copy-label">Copy link</span></button>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>
</article>"""


CATEGORY_COLORS = {
    "Policy":         "#f472b6",
    "Business":       "#fb923c",
    "Research":       "#60a5fa",
    "Infrastructure": "#a78bfa",
    "Society":        "#4ade80",
    "Science":        "#38bdf8",
}


def build_notable_html(item, num):
    cat = item.get("category", "")
    color = CATEGORY_COLORS.get(cat, "#fbbf24")
    return f"""
<article class="notable-card" onclick="toggleNotable(this)">
  <div class="notable-top">
    <div class="notable-meta">
      <span class="notable-cat" style="background:{color}1a;color:{color}">{esc(cat)}</span>
      <span class="notable-src">{esc(item.get('source',''))}</span>
    </div>
    <span class="notable-chevron">&#9660;</span>
  </div>
  <h3 class="notable-headline">{esc(item.get('headline',''))}</h3>
  <p class="notable-summary">{esc(item.get('summary',''))}</p>
  {build_tags_html(item.get('tech_tags', []))}
  <div class="notable-body">
    <div class="notable-apply">
      <div class="blabel">&#x1F4A1; Why This Applies to You</div>
      <p>{esc(item.get('applicability',''))}</p>
      <a class="notable-read" href="{safe_url(item.get('source_url','#'))}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">
        Read original &#x2192;
      </a>
    </div>
  </div>
</article>"""


def generate_html(data):
    # esc() applied to today: in --rebuild mode it comes from disk (digest.json),
    # not strftime, so it must be treated as untrusted input.
    today    = esc(data.get("date", ""))
    ai_html  = "\n".join(build_story_html(s, "#818cf8", i+1, f"story-ai-{i+1}")   for i, s in enumerate(data.get("ai_stories", [])))
    cy_html  = "\n".join(build_story_html(s, "#34d399", i+1, f"story-cyber-{i+1}") for i, s in enumerate(data.get("cyber_stories", [])))
    not_html = "\n".join(build_notable_html(item, i+1) for i, item in enumerate(data.get("notables", [])))

    # OG tags: use the first AI story headline/tldr as the page preview
    first_ai   = data.get("ai_stories", [{}])[0]
    og_title   = esc(f"The Daily Rundown — {today}")
    og_desc    = esc(first_ai.get("tldr", "AI, cybersecurity, and the stories that actually matter — digested daily by Claude."))
    og_url     = esc(PAGES_URL)

    return (HTML_TEMPLATE
            .replace("__DATE__",       today)
            .replace("__OG_TITLE__",   og_title)
            .replace("__OG_DESC__",    og_desc)
            .replace("__OG_URL__",     og_url)
            .replace("__WORKER_URL__", WORKER_URL)
            .replace("__CONNECT_SRC__", WORKER_URL if WORKER_URL else "'none'")
            .replace("__AI_STORIES__",    ai_html)
            .replace("__CYBER_STORIES__", cy_html)
            .replace("__NOTABLES__",      not_html))


def send_email(data):
    today       = esc(data.get("date", ""))
    ai_items    = "".join(f"<li><strong>{esc(s.get('headline',''))}</strong> &mdash; {esc(s.get('tldr',''))}</li>" for s in data.get("ai_stories", []))
    cyber_items = "".join(f"<li><strong>{esc(s.get('headline',''))}</strong> &mdash; {esc(s.get('tldr',''))}</li>" for s in data.get("cyber_stories", []))
    notables_items = "".join(
        f"<li><span style='color:#94a3b8;font-size:0.75rem'>[{esc(n.get('category',''))}]</span> <strong>{esc(n.get('headline',''))}</strong></li>"
        for n in data.get("notables", [])
    )

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">
  <div style="text-align:center;padding:28px 0 24px;border-bottom:1px solid #2d3148">
    <h1 style="margin:0;font-size:1.8rem;font-weight:800;background:linear-gradient(90deg,#818cf8,#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">The Daily Rundown</h1>
    <p style="color:#94a3b8;margin:8px 0 0;font-size:0.9rem">{today}</p>
  </div>
  <div style="padding:24px 0">
    <h2 style="color:#818cf8;font-size:0.78rem;text-transform:uppercase;letter-spacing:1.2px;margin:0 0 12px">AI &amp; Technology</h2>
    <ul style="padding-left:18px;margin:0;line-height:2.2;font-size:0.93rem">{ai_items}</ul>
    <h2 style="color:#34d399;font-size:0.78rem;text-transform:uppercase;letter-spacing:1.2px;margin:28px 0 12px">Cybersecurity</h2>
    <ul style="padding-left:18px;margin:0;line-height:2.2;font-size:0.93rem">{cyber_items}</ul>
    <h2 style="color:#fbbf24;font-size:0.78rem;text-transform:uppercase;letter-spacing:1.2px;margin:28px 0 12px">Notables</h2>
    <ul style="padding-left:18px;margin:0;line-height:2.2;font-size:0.93rem">{notables_items}</ul>
  </div>
  <div style="text-align:center;padding:28px;background:#1a1d2e;border-radius:12px">
    <p style="color:#94a3b8;margin:0 0 20px;font-size:0.93rem">Get concepts, diagrams, insights &amp; deep dives in the full interactive digest</p>
    <a href="{PAGES_URL}" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#059669);color:#fff;text-decoration:none;padding:14px 36px;border-radius:10px;font-weight:700;font-size:1rem">Read Full Digest &#x2192;</a>
  </div>
  <p style="text-align:center;color:#475569;font-size:0.78rem;margin-top:24px">
    Generated with Claude Opus &middot; <a href="https://github.com/dizchrisctrl/daily-digest" style="color:#818cf8;text-decoration:none">daily-digest</a>
  </p>
</div></body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"The Daily Rundown -- {today}"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print("  Email sent via Gmail API (send-only scope)")


GUIDE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>How to Read The Daily Rundown</title>
<style>
:root {
  --bg: #0b0d16; --surface: #12152a; --surface2: #1a1d32; --surface3: #20233c;
  --text: #eaedf5; --muted: #7a849a; --muted2: #5a6275;
  --ai: #818cf8; --cyber: #34d399; --amber: #fbbf24; --purple: #a78bfa; --red: #f87171;
  --border: #252840; --border2: #333660;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.7; }
.page { max-width: 820px; margin: 0 auto; padding: 40px 24px 100px; }

/* Hero */
.hero { text-align: center; padding: 56px 0 44px; border-bottom: 1px solid var(--border); margin-bottom: 56px; }
.hero-eyebrow { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 3px; color: var(--muted2); margin-bottom: 16px; }
.hero h1 { font-size: 2.4rem; font-weight: 900; letter-spacing: -1.5px; line-height: 1.1;
  background: linear-gradient(130deg, var(--ai), var(--purple), var(--cyber));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; margin-bottom: 18px; }
.hero-desc { color: var(--muted); font-size: 1rem; max-width: 560px; margin: 0 auto 28px; line-height: 1.7; }
.back-link { display: inline-flex; align-items: center; gap: 6px; color: var(--muted2); font-size: 0.82rem; text-decoration: none;
  border: 1px solid var(--border); border-radius: 20px; padding: 5px 16px; transition: color 0.2s, border-color 0.2s; }
.back-link:hover { color: var(--ai); border-color: var(--ai); }

/* Section header */
.sec-head { margin: 56px 0 24px; }
.sec-label { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 3px; color: var(--muted2); margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
.sec-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.sec-title { font-size: 1.2rem; font-weight: 800; margin-bottom: 6px; }
.sec-desc { color: var(--muted); font-size: 0.92rem; }

/* Tab pills */
.tabs-demo { display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0; }
.tab-pill { display: inline-flex; align-items: center; gap: 7px; padding: 8px 18px; border-radius: 30px; font-size: 0.82rem; font-weight: 700; border: 2px solid; cursor: default; }
.tab-ai    { background: rgba(129,140,248,0.1); color: var(--ai);    border-color: var(--ai); }
.tab-cyber { background: rgba(52,211,153,0.1);  color: var(--cyber); border-color: var(--cyber); }
.tab-not   { background: rgba(251,191,36,0.1);  color: var(--amber); border-color: var(--amber); }

/* Story card mock */
.card-mock { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; margin: 20px 0; }
.card-mock-header { padding: 16px 20px; background: var(--surface2); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }
.card-mock-badge { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; padding: 3px 10px; border-radius: 12px; }
.card-mock-title { font-size: 0.97rem; font-weight: 700; }
.card-mock-tldr { font-size: 0.82rem; color: var(--muted); margin-top: 4px; }

/* Glossary grid */
.glossary { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; margin: 24px 0; }
.gcard { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }
.gcard-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.gcard-icon { font-size: 1.4rem; flex-shrink: 0; }
.gcard-name { font-size: 0.95rem; font-weight: 800; }
.gcard-sub  { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted2); margin-top: 1px; }
.gcard-desc { font-size: 0.87rem; color: var(--muted); line-height: 1.65; }
.gcard-accent { border-left: 3px solid; }
.accent-ai     { border-color: var(--ai); }
.accent-cyber  { border-color: var(--cyber); }
.accent-amber  { border-color: var(--amber); }
.accent-purple { border-color: var(--purple); }
.accent-red    { border-color: var(--red); }
.accent-green  { border-color: var(--cyber); }
.accent-muted  { border-color: var(--border2); }

/* Sub-section indent */
.subsections { margin: 10px 0 0 20px; display: flex; flex-direction: column; gap: 8px; }
.subcard { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
.subcard-name { font-size: 0.82rem; font-weight: 700; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; }
.subcard-desc { font-size: 0.8rem; color: var(--muted); line-height: 1.55; }

/* Lens chips */
.lens-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 0; }
.lens-chip { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; padding: 3px 9px; border-radius: 20px; }
.lens-sci  { background: rgba(129,140,248,0.15); color: var(--ai); }
.lens-hist { background: rgba(251,191,36,0.15);  color: var(--amber); }
.lens-soc  { background: rgba(52,211,153,0.15);  color: var(--cyber); }

/* Flow */
.flow { display: flex; flex-wrap: wrap; gap: 0; margin: 24px 0; align-items: center; justify-content: center; }
.flow-step { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; flex: 1; min-width: 100px; text-align: center; }
.flow-icon { font-size: 1.5rem; display: block; margin-bottom: 5px; }
.flow-lbl  { font-size: 0.75rem; font-weight: 700; color: var(--text); }
.flow-sub  { font-size: 0.68rem; color: var(--muted); margin-top: 2px; }
.flow-arr  { color: var(--muted2); font-size: 1.1rem; padding: 0 4px; flex-shrink: 0; }

@media (max-width: 600px) {
  .hero h1 { font-size: 1.8rem; }
  .glossary { grid-template-columns: 1fr; }
  .flow-arr { transform: rotate(90deg); }
}
</style>
</head>
<body>
<div class="page">

  <div class="hero">
    <div class="hero-eyebrow">Reader Guide</div>
    <h1>The Daily Rundown</h1>
    <p class="hero-desc">Every morning, an AI reads the day's tech and security news, distills what actually matters, and turns it into a structured digest built for people who want depth — not just headlines. Here's exactly what you're looking at and why each section exists.</p>
    <a href="index.html" class="back-link">&#x2190; Back to today's digest</a>
  </div>


  <!-- HOW IT WORKS -->
  <div class="sec-head">
    <div class="sec-label">01 &nbsp; How it works</div>
    <div class="sec-title">From the internet to your digest</div>
    <div class="sec-desc">Every morning at 7 AM EST, a GitHub Actions workflow wakes up, reads the latest articles from curated RSS feeds, hands them to Claude, and builds this page — automatically.</div>
  </div>

  <div class="flow">
    <div class="flow-step"><span class="flow-icon">📡</span><div class="flow-lbl">RSS Feeds</div><div class="flow-sub">AI, Cyber, Notable</div></div>
    <div class="flow-arr">→</div>
    <div class="flow-step"><span class="flow-icon">🐍</span><div class="flow-lbl">Python</div><div class="flow-sub">Fetch &amp; clean</div></div>
    <div class="flow-arr">→</div>
    <div class="flow-step"><span class="flow-icon">🤖</span><div class="flow-lbl">Claude AI</div><div class="flow-sub">Analyze &amp; write</div></div>
    <div class="flow-arr">→</div>
    <div class="flow-step"><span class="flow-icon">📄</span><div class="flow-lbl">This Page</div><div class="flow-sub">Built &amp; deployed</div></div>
    <div class="flow-arr">→</div>
    <div class="flow-step"><span class="flow-icon">📬</span><div class="flow-lbl">Email</div><div class="flow-sub">Summary sent</div></div>
  </div>


  <!-- THE THREE TABS -->
  <div class="sec-head">
    <div class="sec-label">02 &nbsp; The three sections</div>
    <div class="sec-title">What each tab covers</div>
    <div class="sec-desc">The digest is split into three tabs. Each has a distinct focus and a different set of sources.</div>
  </div>

  <div class="tabs-demo">
    <div class="tab-pill tab-ai">🤖 AI &amp; Technology</div>
    <div class="tab-pill tab-cyber">🔐 Cybersecurity</div>
    <div class="tab-pill tab-not">⚡ Notables</div>
  </div>

  <div class="glossary">
    <div class="gcard gcard-accent accent-ai">
      <div class="gcard-header"><div class="gcard-icon">🤖</div><div><div class="gcard-name">AI &amp; Technology</div><div class="gcard-sub">3 deep-dive stories</div></div></div>
      <div class="gcard-desc">The most significant AI and tech developments of the day — model releases, research breakthroughs, product launches, and infrastructure shifts. Each story gets the full treatment: concept explanation, diagram, public opinion, insights, and a deep dive.</div>
    </div>
    <div class="gcard gcard-accent accent-cyber">
      <div class="gcard-header"><div class="gcard-icon">🔐</div><div><div class="gcard-name">Cybersecurity</div><div class="gcard-sub">3 deep-dive stories</div></div></div>
      <div class="gcard-desc">The day's most important security stories — vulnerabilities, breaches, threat actor activity, and defensive developments. Sources include Krebs on Security, BleepingComputer, The Hacker News, SANS ISC, and Dark Reading.</div>
    </div>
    <div class="gcard gcard-accent accent-amber">
      <div class="gcard-header"><div class="gcard-icon">⚡</div><div><div class="gcard-name">Notables</div><div class="gcard-sub">Quick-read cards</div></div></div>
      <div class="gcard-desc">Broader stories from policy, business, research, and society that matter to anyone working in or near tech. Lighter format — headline, summary, and a note on why it's relevant to your work. Sources include The Verge, Wired, Reuters, and IEEE Spectrum.</div>
    </div>
  </div>


  <!-- STORY ANATOMY -->
  <div class="sec-head">
    <div class="sec-label">03 &nbsp; Inside each story</div>
    <div class="sec-title">A complete guide to every section</div>
    <div class="sec-desc">Each AI and Cybersecurity story is structured as a layered deep-dive. Click a story headline to expand it and find these sections inside.</div>
  </div>

  <div class="glossary">

    <div class="gcard gcard-accent accent-muted">
      <div class="gcard-header"><div class="gcard-icon">⚡</div><div><div class="gcard-name">TL;DR</div><div class="gcard-sub">Always visible</div></div></div>
      <div class="gcard-desc">A single sentence that tells you the most important thing about the story before you open it. If that's all you have time for, this is it.</div>
    </div>

    <div class="gcard gcard-accent accent-ai">
      <div class="gcard-header"><div class="gcard-icon">📌</div><div><div class="gcard-name">Why It Matters</div><div class="gcard-sub">First section inside</div></div></div>
      <div class="gcard-desc">The practical stakes — who is affected, what changes, and why someone working in tech or security should care. Skips the hype and goes straight to consequence.</div>
    </div>

    <div class="gcard gcard-accent accent-purple">
      <div class="gcard-header"><div class="gcard-icon">🧠</div><div><div class="gcard-name">Concept</div><div class="gcard-sub">4-paragraph explainer</div></div></div>
      <div class="gcard-desc">A structured explanation of the core technology or idea behind the story. Paragraph 1 gives a real-world analogy. Paragraph 2 explains how it technically works. Paragraph 3 ties it to this specific story. Paragraph 4 covers broader implications.</div>
    </div>

    <div class="gcard gcard-accent accent-ai">
      <div class="gcard-header"><div class="gcard-icon">📊</div><div><div class="gcard-name">Visual Diagram</div><div class="gcard-sub">SVG illustration</div></div></div>
      <div class="gcard-desc">An SVG diagram generated by Claude to illustrate the concept — attack chains, data flows, architecture maps, timelines, or comparisons. Chosen based on what best communicates the structure of the idea.</div>
    </div>

    <div class="gcard gcard-accent accent-green">
      <div class="gcard-header"><div class="gcard-icon">👥</div><div><div class="gcard-name">Public Opinion</div><div class="gcard-sub">Community reactions</div></div></div>
      <div class="gcard-desc">What Hacker News, Reddit (r/technology, r/netsec), and Security Twitter are actually saying about this story. Each community gets its own collapsible entry — click to read the full sentiment.</div>
      <div class="subsections">
        <div class="subcard">
          <div class="subcard-name">📊 Sentiment Summary</div>
          <div class="subcard-desc">A 2-3 sentence synthesis of the dominant collective mood across all communities — the shared concern, excitement, or skepticism.</div>
        </div>
        <div class="subcard">
          <div class="subcard-name">😈 Devil's Advocate</div>
          <div class="subcard-desc">A sharp counter-perspective that challenges the dominant public sentiment — an overlooked irony, an inconvenient truth, or a reframe that makes you reconsider the story.</div>
        </div>
      </div>
    </div>

    <div class="gcard gcard-accent accent-ai">
      <div class="gcard-header"><div class="gcard-icon">💡</div><div><div class="gcard-name">Insights</div><div class="gcard-sub">3 expandable cards</div></div></div>
      <div class="gcard-desc">Three perspective cards that add conceptual depth to the story. Each is written through a distinct lens — click any card to reveal the insight and its explanation.</div>
      <div class="lens-row">
        <span class="lens-chip lens-sci">🔬 Scientific — how it works</span>
        <span class="lens-chip lens-hist">📜 Historical — what it echoes</span>
        <span class="lens-chip lens-soc">🌍 Societal — who it affects</span>
      </div>
    </div>

    <div class="gcard gcard-accent accent-purple">
      <div class="gcard-header"><div class="gcard-icon">💭</div><div><div class="gcard-name">Deep Dive</div><div class="gcard-sub">Closing narrative</div></div></div>
      <div class="gcard-desc">The richest section of each story — a layered conclusion that synthesizes everything covered and leaves you with something to think about.</div>
      <div class="subsections">
        <div class="subcard">
          <div class="subcard-name">💭 Narrative</div>
          <div class="subcard-desc">A riveting 3-4 sentence thread that weaves together the concept, the event, and the stakes — written like the opening of a great longform piece.</div>
        </div>
        <div class="subcard">
          <div class="subcard-name">🎯 How This Affects You</div>
          <div class="subcard-desc">Specific and personal — how this story connects to your day-to-day work, your tools, your security posture, or your career.</div>
        </div>
        <div class="subcard">
          <div class="subcard-name">🔭 Outlook</div>
          <div class="subcard-desc">A forward-looking conclusion: what this story likely accelerates or disrupts, and what to watch for in the coming weeks or months.</div>
        </div>
      </div>
    </div>

    <div class="gcard gcard-accent accent-amber">
      <div class="gcard-header"><div class="gcard-icon">🏷️</div><div><div class="gcard-name">Tech Tags</div><div class="gcard-sub">Contextual tooltips</div></div></div>
      <div class="gcard-desc">Up to 3 specific technologies, protocols, or CVE IDs that appear in the story. Each tag includes a description of what it is and why it's relevant here — hover or tap to read. Never generic terms.</div>
    </div>

  </div>


  <!-- NOTABLES FORMAT -->
  <div class="sec-head">
    <div class="sec-label">04 &nbsp; Notables format</div>
    <div class="sec-title">The lighter-format cards</div>
    <div class="sec-desc">Notable cards cover broader stories from policy, business, research, and society. Lighter than the deep-dive format — designed for a quick read.</div>
  </div>

  <div class="glossary">
    <div class="gcard gcard-accent accent-amber">
      <div class="gcard-header"><div class="gcard-icon">📰</div><div><div class="gcard-name">Summary</div></div></div>
      <div class="gcard-desc">2-3 sentences covering what happened and why it's significant.</div>
    </div>
    <div class="gcard gcard-accent accent-amber">
      <div class="gcard-header"><div class="gcard-icon">💡</div><div><div class="gcard-name">Why This Applies to You</div></div></div>
      <div class="gcard-desc">How this story could matter to someone in tech or security — career implications, tools to watch, policy awareness, or market shifts.</div>
    </div>
  </div>

  <div style="text-align:center; margin-top: 60px; padding-top: 32px; border-top: 1px solid var(--border);">
    <a href="index.html" style="display:inline-block; background: linear-gradient(135deg, #4f46e5, #059669); color:#fff; text-decoration:none; padding:13px 32px; border-radius:10px; font-weight:700; font-size:0.95rem;">&#x2190; Back to today's digest</a>
  </div>

</div>
</body>
</html>"""


def _story_redirect_html(story, story_id):
    """Thin page with story-specific OG tags that redirects to the main digest anchor."""
    headline = story.get('headline', 'The Daily Rundown')
    tldr     = story.get('tldr', '')
    source   = story.get('source', '')
    dest_url = f"{PAGES_URL}#{story_id}"
    page_url = f"{PAGES_URL}/s/{story_id}.html"

    def _attr(s):
        return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_attr(headline)} — The Daily Rundown</title>
<meta property="og:type"        content="article">
<meta property="og:site_name"   content="The Daily Rundown">
<meta property="og:title"       content="{_attr(headline)}">
<meta property="og:description" content="{_attr(tldr)}">
<meta property="og:url"         content="{_attr(page_url)}">
<meta name="twitter:card"        content="summary">
<meta name="twitter:title"       content="{_attr(headline)}">
<meta name="twitter:description" content="{_attr(tldr)}">
{f'<meta name="author" content="{_attr(source)}">' if source else ''}
<meta http-equiv="refresh" content="0; url={_attr(dest_url)}">
<script>window.location.replace("{dest_url.replace('"', '\\"')}");</script>
</head>
<body></body>
</html>"""


def _write_story_pages(data):
    """Write per-story redirect pages with story-specific OG tags to output/s/."""
    os.makedirs("output/s", exist_ok=True)
    count = 0
    for i, story in enumerate(data.get("ai_stories", []), 1):
        story_id = f"story-ai-{i}"
        with open(f"output/s/{story_id}.html", "w", encoding="utf-8") as f:
            f.write(_story_redirect_html(story, story_id))
        count += 1
    for i, story in enumerate(data.get("cyber_stories", []), 1):
        story_id = f"story-cyber-{i}"
        with open(f"output/s/{story_id}.html", "w", encoding="utf-8") as f:
            f.write(_story_redirect_html(story, story_id))
        count += 1
    return count


def save_output(html, data):
    os.makedirs("output", exist_ok=True)
    with open("output/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("output/digest.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open("output/guide.html", "w", encoding="utf-8") as f:
        f.write(GUIDE_HTML)
    n = _write_story_pages(data)
    print(f"  Saved: output/index.html + output/digest.json + output/guide.html + {n} story pages")


if __name__ == "__main__":
    import sys
    rebuild_only = "--rebuild" in sys.argv

    print("[ The Daily Rundown Generator ]")

    if rebuild_only:
        print("\n-> Rebuild mode: loading existing digest.json...")
        with open("output/digest.json", encoding="utf-8") as f:
            data = json.load(f)
        print(f"  Stories -- AI: {len(data.get('ai_stories',[]))} | Cyber: {len(data.get('cyber_stories',[]))} | Notables: {len(data.get('notables',[]))}")
        print("\n-> Building HTML...")
        html = generate_html(data)
        os.makedirs("output", exist_ok=True)
        with open("output/index.html", "w", encoding="utf-8") as f:
            f.write(html)
        with open("output/guide.html", "w", encoding="utf-8") as f:
            f.write(GUIDE_HTML)
        n = _write_story_pages(data)
        print(f"  Saved: output/index.html + output/guide.html + {n} story pages")
        print("\nDone (rebuild only -- no email sent)!")
    else:
        print("\n-> Fetching news...")
        ai_articles       = fetch_articles(AI_FEEDS,       max_per_feed=2, total_limit=10)
        cyber_articles    = fetch_articles(CYBER_FEEDS,    max_per_feed=2, total_limit=12)
        notables_articles = fetch_articles(NOTABLES_FEEDS, max_per_feed=2, total_limit=14)
        print(f"  AI: {len(ai_articles)} | Cyber: {len(cyber_articles)} | Notables pool: {len(notables_articles)}")

        print("\n-> Generating with Claude Opus...")
        data = generate_digest_json(ai_articles, cyber_articles, notables_articles)
        print(f"  Stories -- AI: {len(data.get('ai_stories',[]))} | Cyber: {len(data.get('cyber_stories',[]))} | Notables: {len(data.get('notables',[]))}")

        print("\n-> Building HTML...")
        html = generate_html(data)

        print("\n-> Saving output...")
        save_output(html, data)

        print("\n-> Sending email...")
        send_email(data)

        print("\nDone!")
