#!/usr/bin/env python3
"""Daily Tech & Cybersecurity Digest Generator"""

import os
import re
import json
import base64
import feedparser
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────────
RECIPIENT_EMAIL = "Diazz.christian@gmail.com"
SENDER_EMAIL    = os.environ["GMAIL_ADDRESS"]
PAGES_URL       = "https://dizchrisctrl.github.io/daily-digest"

GMAIL_CLIENT_ID     = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

# ── RSS Feeds ──────────────────────────────────────────────────────────────────
AI_FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/feed/",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
]

CYBER_FEEDS = [
    "https://krebsonsecurity.com/feed/",
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.bleepingcomputer.com/feed/",
    "https://isc.sans.edu/rssfeed_full.xml",
]

NOTABLES_FEEDS = [
    "https://www.theverge.com/rss/index.xml",
    "https://www.wired.com/feed/rss",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://hnrss.org/frontpage",
    "https://spectrum.ieee.org/feeds/feed.rss",
]


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def fetch_articles(feeds, max_per_feed=2, total_limit=8):
    articles = []
    for url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "DailyDigest/1.0"})
            for entry in feed.entries[:max_per_feed]:
                summary = strip_html(entry.get("summary", entry.get("description", "")))[:600]
                articles.append({
                    "title":   strip_html(entry.get("title", "Untitled")),
                    "summary": summary,
                    "link":    entry.get("link", "#"),
                    "source":  strip_html(feed.feed.get("title", "Unknown Source")),
                })
        except Exception as e:
            print(f"  Feed error [{url}]: {e}")
    return articles[:total_limit]


# ── Story schema (AI + Cyber deep-dive cards) ──────────────────────────────────
STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":          {"type": "string", "description": "Short punchy headline"},
        "tldr":              {"type": "string", "description": "One sentence that tells the whole story"},
        "why_it_matters":    {"type": "string", "description": "2-3 sentences on real-world significance"},
        "concept_title":     {"type": "string", "description": "The core technical concept illustrated"},
        "concept_explained": {"type": "string", "description": "4 paragraphs separated by newlines. P1: simple real-world analogy. P2: how it technically works. P3: tie to this news story. P4: broader implications."},
        "visual_ascii":      {"type": "string", "description": "ASCII diagram 15-25 lines using box-drawing chars. Genuinely informative, not decorative."},
        "public_opinion":    {"type": "string", "description": "Concrete sentiments from HN, Reddit, security Twitter — what communities are saying"},
        "opinion_assessment":{"type": "string", "description": "Critical analysis: what's valid, overblown, or missing from that public opinion"},
        "quiz": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q":       {"type": "string"},
                    "a":       {"type": "string"},
                    "explain": {"type": "string"},
                },
                "required": ["q", "a", "explain"],
            },
            "minItems": 3,
            "maxItems": 3,
        },
        "deep_dive":  {"type": "string", "description": "Socratic question connecting this to bigger trends"},
        "source_url": {"type": "string"},
        "source":     {"type": "string"},
    },
    "required": ["headline","tldr","why_it_matters","concept_title","concept_explained",
                 "visual_ascii","public_opinion","opinion_assessment","quiz","deep_dive",
                 "source_url","source"],
}

SECTION_TOOL = {
    "name": "publish_stories",
    "description": "Publish 3 formatted stories for one digest section",
    "input_schema": {
        "type": "object",
        "properties": {
            "stories": {"type": "array", "items": STORY_SCHEMA, "minItems": 3, "maxItems": 3},
        },
        "required": ["stories"],
    },
}

SECTION_PROMPT = """Today is {today}. Create 3 digest stories for someone moderately technical — works in or near tech/security, understands concepts, appreciates clear explanations with real depth.

NEWS ARTICLES (pick the 3 most notable):
{articles}

For each story:
- concept_explained: 4 paragraphs. P1: simple real-world analogy. P2: how it technically works. P3: tie to this story. P4: broader implications.
- visual_ascii: meaningful diagram using box-drawing chars (+-|<>^v). 15-25 lines. Labels required.
- quiz: 3 questions testing real conceptual understanding, not trivia.
- public_opinion: reference specific community sentiments (HN, Reddit r/technology, r/netsec, security Twitter/X).
- deep_dive: a Socratic question forcing critical thinking about assumptions or bigger trends.

Call the publish_stories tool with your 3 stories."""


# ── Notables schema (broader news highlights with applicability) ───────────────
NOTABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":      {"type": "string", "description": "Short punchy headline"},
        "summary":       {"type": "string", "description": "2-3 sentences covering what happened and why it is significant"},
        "applicability": {"type": "string", "description": "2-3 sentences on how this could matter to someone in tech or security — career implications, tools to watch, policy awareness, market shifts"},
        "category":      {"type": "string", "description": "One of: Policy, Business, Research, Infrastructure, Society, Science"},
        "source_url":    {"type": "string"},
        "source":        {"type": "string"},
    },
    "required": ["headline", "summary", "applicability", "category", "source_url", "source"],
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

Prioritize stories with genuine weight. Avoid minor product launches or clickbait.
Call the publish_notables tool with your 5 items."""


# ── Claude calls ───────────────────────────────────────────────────────────────
def call_claude_for_section(client, today, articles):
    prompt = SECTION_PROMPT.format(today=today, articles=json.dumps(articles, indent=2))
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        tools=[SECTION_TOOL],
        tool_choice={"type": "tool", "name": "publish_stories"},
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].input["stories"]


def call_claude_for_notables(client, today, articles):
    prompt = NOTABLES_PROMPT.format(today=today, articles=json.dumps(articles, indent=2))
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        tools=[NOTABLES_TOOL],
        tool_choice={"type": "tool", "name": "publish_notables"},
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].input["items"]


def generate_digest_json(ai_articles, cyber_articles, notables_articles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now(timezone.utc).strftime("%B %d, %Y")

    print("  -> Generating AI stories...")
    ai_stories = call_claude_for_section(client, today, ai_articles)
    print("  -> Generating Cybersecurity stories...")
    cyber_stories = call_claude_for_section(client, today, cyber_articles)
    print("  -> Generating Notables...")
    notables = call_claude_for_notables(client, today, notables_articles)

    return {"date": today, "ai_stories": ai_stories, "cyber_stories": cyber_stories, "notables": notables}


# ── HTML Template ──────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Digest -- __DATE__</title>
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
  background: linear-gradient(180deg, #161928 0%, var(--bg) 100%);
}
.site-header::before {
  content: '';
  position: absolute; inset: 0;
  background: radial-gradient(ellipse 70% 60% at 50% 0%, rgba(129,140,248,0.12) 0%, transparent 70%);
  pointer-events: none;
}
.eyebrow { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 4px; color: var(--muted2); margin-bottom: 14px; }
.site-header h1 {
  font-size: 2.8rem; font-weight: 900; letter-spacing: -2px; line-height: 1;
  background: linear-gradient(130deg, var(--ai) 0%, var(--purple) 45%, var(--cyber) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  margin-bottom: 14px;
}
.date-badge {
  display: inline-flex; align-items: center; gap: 8px;
  color: var(--muted); font-size: 0.82rem; padding: 5px 16px;
  border: 1px solid var(--border2); border-radius: 20px;
  background: var(--surface2);
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
.story-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  margin-bottom: 12px;
  overflow: hidden;
  transition: border-color 0.25s, box-shadow 0.25s;
}
.story-card:hover { border-color: var(--border2); box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
.story-card.open { border-color: var(--border2); box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
.story-card.kbd-focus { border-color: var(--ai) !important; box-shadow: 0 0 0 2px rgba(129,140,248,0.25) !important; }

/* Summary row */
.story-summary {
  padding: 20px 22px; cursor: pointer;
  display: flex; align-items: flex-start; gap: 14px;
  transition: background 0.15s; user-select: none;
}
.story-summary:hover { background: rgba(255,255,255,0.025); }
.s-left { flex: 1; min-width: 0; }
.s-meta { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; }
.src-badge { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; padding: 2px 9px; border-radius: 20px; }
.story-num { font-size: 0.68rem; font-weight: 700; color: var(--muted2); font-variant-numeric: tabular-nums; }
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
.block p { font-size: 0.93rem; line-height: 1.75; color: #c0c8d8; }

/* Concept block */
.concept-block { background: linear-gradient(180deg, #171b30 0%, #141828 100%); }
.concept-title { font-size: 0.97rem; font-weight: 700; margin-bottom: 14px; }
.concept-text p { font-size: 0.93rem; line-height: 1.8; color: #b8c2d4; margin-bottom: 13px; }
.concept-text p:last-child { margin-bottom: 0; }

/* Terminal / ASCII */
.terminal { border-radius: 10px; overflow: hidden; border: 1px solid #1e3055; box-shadow: 0 0 40px rgba(125,211,252,0.06); }
.term-bar { background: #0d1020; padding: 9px 14px; display: flex; align-items: center; gap: 7px; border-bottom: 1px solid #1e3055; }
.dot { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }
.dot-r { background: #ff5f57; } .dot-y { background: #febc2e; } .dot-g { background: #28c840; }
.term-title { flex: 1; text-align: center; font-size: 0.67rem; color: #3a4a60; font-family: monospace; }
pre.ascii {
  font-family: 'Courier New', Courier, monospace;
  font-size: 0.74rem; line-height: 1.5;
  color: #7dd3fc; padding: 16px 18px;
  background: #060912;
  overflow-x: auto; white-space: pre;
  text-shadow: 0 0 12px rgba(125,211,252,0.4);
}

/* Opinion block */
.opinion-block { background: #0d160e; }
.opinion-q { font-style: italic; color: var(--muted); padding: 11px 15px; border-left: 3px solid rgba(52,211,153,0.4); margin-bottom: 14px; font-size: 0.91rem; line-height: 1.7; border-radius: 0 6px 6px 0; background: rgba(52,211,153,0.04); }

/* Quiz */
.quiz-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
.qcard {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px; cursor: pointer;
  transition: border-color 0.2s, transform 0.15s, box-shadow 0.15s;
  user-select: none;
}
.qcard:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.3); border-color: var(--border2); }
.qcard.open { border-color: var(--cyber); }
.q-num { font-size: 0.63rem; font-weight: 700; color: var(--muted2); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 7px; }
.q-text { font-weight: 600; font-size: 0.87rem; line-height: 1.5; color: var(--text); }
.q-answer { max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.35s ease, opacity 0.3s ease; }
.qcard.open .q-answer { max-height: 400px; opacity: 1; }
.q-divider { height: 1px; background: var(--border); margin: 10px 0; }
.q-ans { color: var(--cyber); font-weight: 600; font-size: 0.85rem; margin-bottom: 5px; }
.q-exp { color: var(--muted); font-style: italic; font-size: 0.79rem; line-height: 1.55; }
.q-hint { font-size: 0.65rem; color: var(--muted2); margin-top: 8px; display: flex; align-items: center; gap: 4px; }
.qcard.open .q-hint { color: var(--cyber2); }

/* Deep Dive */
.deepdive-block { background: linear-gradient(135deg, #110d22 0%, #0e0c1e 100%); position: relative; overflow: hidden; }
.deepdive-block::after { content: '"'; position: absolute; right: 18px; top: 8px; font-size: 6rem; color: rgba(167,139,250,0.07); font-family: Georgia, serif; line-height: 1; }
.deepdive-text { font-size: 1.02rem; font-style: italic; color: #c4b5fd; padding-left: 16px; border-left: 3px solid var(--purple); line-height: 1.85; }

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
.notable-apply p { font-size: 0.88rem; color: #c0c8d8; line-height: 1.7; }
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

/* Site footer */
.site-footer { text-align: center; padding: 40px 20px; color: var(--muted2); font-size: 0.8rem; border-top: 1px solid var(--border); }
.site-footer a { color: var(--ai); text-decoration: none; }

/* Mobile */
@media (max-width: 640px) {
  .site-header h1 { font-size: 2rem; letter-spacing: -1px; }
  .story-summary { padding: 16px; gap: 10px; }
  .story-summary h2 { font-size: 0.97rem; }
  .block { padding: 16px; }
  pre.ascii { font-size: 0.6rem; padding: 12px; }
  .quiz-grid { grid-template-columns: 1fr; }
  .notable-grid { grid-template-columns: 1fr; }
  .tab-btn { font-size: 0.78rem; padding: 13px 8px; gap: 5px; }
  #kbd-hint { display: none; }
}
</style>
</head>
<body>

<div id="progress-bar"></div>

<header class="site-header">
  <div class="eyebrow">Your daily briefing</div>
  <h1>Daily Digest</h1>
  <div class="date-badge"><span class="date-dot"></span>__DATE__</div>
</header>

<div class="tabs-wrap">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('ai',this)">&#x1F916; AI &amp; Technology</button>
    <button class="tab-btn" onclick="switchTab('cyber',this)">&#x1F510; Cybersecurity</button>
    <button class="tab-btn" onclick="switchTab('notables',this)">&#x1F4F0; Notables</button>
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
</main>

<footer class="site-footer">
  Daily Digest &middot; Generated with Claude Opus &middot; <a href="https://github.com/dizchrisctrl/daily-digest">GitHub</a>
</footer>

<div id="kbd-hint">
  <kbd>j</kbd> / <kbd>k</kbd> navigate &nbsp; <kbd>Enter</kbd> expand<br>
  <kbd>1</kbd> AI &nbsp; <kbd>2</kbd> Cyber &nbsp; <kbd>3</kbd> Notables
</div>

<script>
const indicator = document.getElementById('indicator');
const tabColors = { ai: '#818cf8', cyber: '#34d399', notables: '#fbbf24' };
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
});

// ── Story card toggle ──
function toggleStory(card) { card.classList.toggle('open'); }
function toggleCard(card) { card.classList.toggle('open'); }
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

document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, select')) return;

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


def safe_url(url):
    u = str(url).strip()
    if u.lower().startswith(("http://", "https://")):
        return esc(u)
    return "#"


def build_story_html(story, color, num):
    quiz_html = ""
    for i, q in enumerate(story.get("quiz", []), 1):
        quiz_html += f"""
      <div class="qcard" onclick="toggleCard(this)">
        <div class="q-num">Q{i} of 3</div>
        <div class="q-text">{esc(q.get('q',''))}</div>
        <div class="q-answer">
          <div class="q-divider"></div>
          <div class="q-ans">{esc(q.get('a',''))}</div>
          <div class="q-exp">{esc(q.get('explain',''))}</div>
        </div>
        <div class="q-hint">&#9660; tap to reveal</div>
      </div>"""

    concept_paras = "".join(
        f"<p>{esc(p.strip())}</p>"
        for p in story.get("concept_explained", "").split("\n\n")
        if p.strip()
    )

    num_str = f"{num:02d}"

    return f"""
<article class="story-card">
  <div class="story-summary" onclick="toggleStory(this.closest('.story-card'))">
    <div class="s-left">
      <div class="s-meta">
        <span class="src-badge" style="background:{color}1a;color:{color}">{esc(story.get('source',''))}</span>
        <span class="story-num">{num_str}</span>
      </div>
      <h2>{esc(story.get('headline',''))}</h2>
      <div class="tldr"><span class="tldr-tag">TL;DR</span>{esc(story.get('tldr',''))}</div>
    </div>
    <div class="chevron">&#9660;</div>
  </div>

  <div class="story-body">
    <div class="body-inner">

      <div class="block">
        <div class="blabel">&#x1F4CC; Why It Matters</div>
        <p>{esc(story.get('why_it_matters',''))}</p>
      </div>

      <div class="block concept-block">
        <div class="blabel">&#x1F9E0; Concept</div>
        <div class="concept-title" style="color:{color}">{esc(story.get('concept_title',''))}</div>
        <div class="concept-text">{concept_paras}</div>
      </div>

      <div class="block">
        <div class="blabel">&#x1F4CA; Visual Diagram</div>
        <div class="terminal">
          <div class="term-bar">
            <span class="dot dot-r"></span><span class="dot dot-y"></span><span class="dot dot-g"></span>
            <span class="term-title">{esc(story.get('concept_title','diagram'))}</span>
          </div>
          <pre class="ascii">{esc(story.get('visual_ascii',''))}</pre>
        </div>
      </div>

      <div class="block opinion-block">
        <div class="blabel">&#x1F465; Public Opinion</div>
        <div class="opinion-q">{esc(story.get('public_opinion',''))}</div>
        <div class="blabel">&#x1F50D; Assessment</div>
        <p>{esc(story.get('opinion_assessment',''))}</p>
      </div>

      <div class="block">
        <div class="blabel">&#x2753; Quiz Yourself</div>
        <div class="quiz-grid">{quiz_html}</div>
      </div>

      <div class="block deepdive-block">
        <div class="blabel">&#x1F4AD; Deep Dive</div>
        <p class="deepdive-text">{esc(story.get('deep_dive',''))}</p>
      </div>

      <div class="story-footer">
        <a class="src-link" href="{safe_url(story.get('source_url','#'))}" target="_blank" rel="noopener noreferrer">
          Read original <span>&#x2192;</span>
        </a>
        <span class="src-name">{esc(story.get('source',''))}</span>
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
    today    = data["date"]
    ai_html  = "\n".join(build_story_html(s, "#818cf8", i+1) for i, s in enumerate(data.get("ai_stories", [])))
    cy_html  = "\n".join(build_story_html(s, "#34d399", i+1) for i, s in enumerate(data.get("cyber_stories", [])))
    not_html = "\n".join(build_notable_html(item, i+1) for i, item in enumerate(data.get("notables", [])))
    return (HTML_TEMPLATE
            .replace("__DATE__", today)
            .replace("__AI_STORIES__", ai_html)
            .replace("__CYBER_STORIES__", cy_html)
            .replace("__NOTABLES__", not_html))


def send_email(data):
    today       = data["date"]
    ai_items    = "".join(f"<li><strong>{esc(s['headline'])}</strong> &mdash; {esc(s['tldr'])}</li>" for s in data.get("ai_stories", []))
    cyber_items = "".join(f"<li><strong>{esc(s['headline'])}</strong> &mdash; {esc(s['tldr'])}</li>" for s in data.get("cyber_stories", []))
    notables_items = "".join(
        f"<li><span style='color:#94a3b8;font-size:0.75rem'>[{esc(n.get('category',''))}]</span> <strong>{esc(n['headline'])}</strong></li>"
        for n in data.get("notables", [])
    )

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">
  <div style="text-align:center;padding:28px 0 24px;border-bottom:1px solid #2d3148">
    <h1 style="margin:0;font-size:1.8rem;font-weight:800;background:linear-gradient(90deg,#818cf8,#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">Daily Digest</h1>
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
    <p style="color:#94a3b8;margin:0 0 20px;font-size:0.93rem">Get concepts, diagrams, quizzes &amp; deep dives in the full interactive digest</p>
    <a href="{PAGES_URL}" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#059669);color:#fff;text-decoration:none;padding:14px 36px;border-radius:10px;font-weight:700;font-size:1rem">Read Full Digest &#x2192;</a>
  </div>
  <p style="text-align:center;color:#475569;font-size:0.78rem;margin-top:24px">
    Generated with Claude Opus &middot; <a href="https://github.com/dizchrisctrl/daily-digest" style="color:#818cf8;text-decoration:none">daily-digest</a>
  </p>
</div></body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Digest -- {today}"
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


def save_output(html, data):
    os.makedirs("output", exist_ok=True)
    with open("output/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("output/digest.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("  Saved: output/index.html + output/digest.json")


if __name__ == "__main__":
    import sys
    rebuild_only = "--rebuild" in sys.argv

    print("[ Daily Digest Generator ]")

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
        print("  Saved: output/index.html")
        print("\nDone (rebuild only -- no email sent)!")
    else:
        print("\n-> Fetching news...")
        ai_articles       = fetch_articles(AI_FEEDS, max_per_feed=2, total_limit=8)
        cyber_articles    = fetch_articles(CYBER_FEEDS, max_per_feed=2, total_limit=8)
        notables_articles = fetch_articles(NOTABLES_FEEDS, max_per_feed=3, total_limit=12)
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
