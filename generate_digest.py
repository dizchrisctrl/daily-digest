#!/usr/bin/env python3
"""Daily Tech & Cybersecurity Digest Generator"""

import os
import re
import json
import smtplib
import feedparser
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

# ── Config ─────────────────────────────────────────────────────────────────────
RECIPIENT_EMAIL = "Diazz.christian@gmail.com"
SENDER_EMAIL    = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PWD   = os.environ["GMAIL_APP_PASSWORD"]
PAGES_URL       = "https://dizchrisctrl.github.io/daily-digest"

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


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def fetch_articles(feeds, max_per_feed=2):
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
    return articles[:8]


STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":          {"type": "string", "description": "Short punchy headline"},
        "tldr":              {"type": "string", "description": "One sentence that tells the whole story"},
        "why_it_matters":    {"type": "string", "description": "2-3 sentences on real-world significance"},
        "concept_title":     {"type": "string", "description": "The core technical concept illustrated (e.g. 'Retrieval-Augmented Generation')"},
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

DIGEST_TOOL = {
    "name": "publish_digest",
    "description": "Publish the formatted daily digest",
    "input_schema": {
        "type": "object",
        "properties": {
            "date":          {"type": "string"},
            "ai_stories":    {"type": "array", "items": STORY_SCHEMA, "minItems": 3, "maxItems": 3},
            "cyber_stories": {"type": "array", "items": STORY_SCHEMA, "minItems": 3, "maxItems": 3},
        },
        "required": ["date", "ai_stories", "cyber_stories"],
    },
}


def generate_digest_json(ai_articles, cyber_articles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now(timezone.utc).strftime("%B %d, %Y")

    prompt = f"""Today is {today}. Create a premium daily digest for someone moderately technical — works in or near tech/security, understands concepts, appreciates clear explanations with real depth.

AI/TECH NEWS (pick the 3 most notable/interesting):
{json.dumps(ai_articles, indent=2)}

CYBERSECURITY NEWS (pick the 3 most notable/interesting):
{json.dumps(cyber_articles, indent=2)}

For each story:
- concept_explained: 4 paragraphs. P1: simple real-world analogy. P2: how it technically works. P3: tie to this story. P4: broader implications.
- visual_ascii: meaningful diagram using box-drawing chars (┌─┐│└┘├┤→←↑↓). 15-25 lines. Labels required.
- quiz: 3 questions that test real conceptual understanding, not trivia.
- public_opinion: reference specific community sentiments (HN, Reddit r/technology, r/netsec, security Twitter/X).
- deep_dive: a Socratic question that forces critical thinking about assumptions or bigger trends.

Call the publish_digest tool with your response."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        tools=[DIGEST_TOOL],
        tool_choice={"type": "tool", "name": "publish_digest"},
        messages=[{"role": "user", "content": prompt}],
    )

    # tool_use guarantees valid structured JSON — no parsing needed
    return response.content[0].input


# ── HTML Template (uses __PLACEHOLDER__ to avoid f-string brace conflicts) ─────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Digest — __DATE__</title>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d2e; --surface2: #222536;
  --text: #e2e8f0; --muted: #94a3b8;
  --ai: #818cf8; --cyber: #34d399;
  --border: #2d3148; --purple: #7c3aed;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.7; }

.site-header { background: linear-gradient(135deg, #1a1d2e, #0f1117); border-bottom: 1px solid var(--border); padding: 32px 20px; text-align: center; }
.site-header h1 { font-size: 2.4rem; font-weight: 800; letter-spacing: -1px; background: linear-gradient(90deg, var(--ai), var(--cyber)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
.date-badge { display: inline-block; color: var(--muted); font-size: 0.88rem; padding: 4px 14px; border: 1px solid var(--border); border-radius: 20px; margin-top: 10px; }

.tabs { display: flex; background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 100; }
.tab-btn { flex: 1; padding: 16px; background: none; border: none; border-bottom: 3px solid transparent; color: var(--muted); font-size: 0.97rem; font-weight: 600; cursor: pointer; transition: all 0.2s; }
.tab-btn.ai.active { color: var(--ai); border-bottom-color: var(--ai); }
.tab-btn.cyber.active { color: var(--cyber); border-bottom-color: var(--cyber); }

.content { max-width: 860px; margin: 0 auto; padding: 32px 16px; }
.section { display: none; }
.section.active { display: block; }

.story-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; margin-bottom: 32px; overflow: hidden; }

.story-header { padding: 28px; }
.story-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 0.7rem; font-weight: 700; padding: 3px 12px; border-radius: 20px; margin-bottom: 14px; text-transform: uppercase; letter-spacing: 0.8px; }
.story-header h2 { font-size: 1.35rem; font-weight: 700; line-height: 1.35; margin-bottom: 12px; }
.tldr { background: var(--surface2); border-radius: 8px; padding: 12px 16px; font-size: 0.94rem; color: var(--muted); border-left: 3px solid var(--border); }
.tldr strong { color: var(--text); }

.block { padding: 24px 28px; border-top: 1px solid var(--border); }
.blabel { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: var(--muted); margin-bottom: 12px; }

.concept-block { background: #1c1f33; }
.concept-text p { font-size: 0.96rem; margin-bottom: 14px; }
.concept-text p:last-child { margin-bottom: 0; }

pre.ascii { font-family: 'Courier New', monospace; font-size: 0.76rem; line-height: 1.45; background: #080b12; color: #7dd3fc; padding: 20px; border-radius: 10px; overflow-x: auto; white-space: pre; border: 1px solid #1e3a5f; }

.opinion-block { background: #111a11; }
.opinion-quote { font-style: italic; color: var(--muted); padding: 12px 16px; border-left: 3px solid #22c55e44; margin-bottom: 16px; font-size: 0.94rem; }

.quiz-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.qcard { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 16px; cursor: pointer; transition: border-color 0.2s, transform 0.1s; user-select: none; }
.qcard:hover { border-color: var(--ai); transform: translateY(-1px); }
.qcard.open { border-color: var(--cyber); }
.qcard-q { font-weight: 600; font-size: 0.88rem; line-height: 1.5; }
.qcard-a { display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
.qcard-ans { color: var(--cyber); font-weight: 600; font-size: 0.87rem; margin-bottom: 6px; }
.qcard-exp { color: var(--muted); font-style: italic; font-size: 0.82rem; }
.qcard-hint { font-size: 0.7rem; color: var(--muted); margin-top: 8px; }

.deepdive-block { background: #180d28; }
.deepdive-text { font-size: 1.02rem; font-style: italic; color: #c4b5fd; padding-left: 16px; border-left: 3px solid var(--purple); line-height: 1.8; }

.story-footer { padding: 14px 28px; border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.src-link { color: var(--ai); text-decoration: none; font-size: 0.87rem; font-weight: 600; }
.src-link:hover { text-decoration: underline; }
.src-name { color: var(--muted); font-size: 0.8rem; }

.site-footer { text-align: center; padding: 40px 20px; color: var(--muted); font-size: 0.83rem; border-top: 1px solid var(--border); }
.site-footer a { color: var(--ai); text-decoration: none; }

@media (max-width: 640px) {
  .site-header h1 { font-size: 1.7rem; }
  .story-header { padding: 20px; }
  .story-header h2 { font-size: 1.1rem; }
  .block { padding: 18px 20px; }
  pre.ascii { font-size: 0.62rem; padding: 12px; }
  .quiz-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<header class="site-header">
  <h1>Daily Digest</h1>
  <div class="date-badge">__DATE__</div>
</header>

<nav class="tabs">
  <button class="tab-btn ai active" onclick="switchTab('ai',this)">🤖 AI &amp; Technology</button>
  <button class="tab-btn cyber" onclick="switchTab('cyber',this)">🔐 Cybersecurity</button>
</nav>

<main class="content">
  <section id="ai" class="section active">__AI_STORIES__</section>
  <section id="cyber" class="section">__CYBER_STORIES__</section>
</main>

<footer class="site-footer">
  Daily Digest &middot; Generated with Claude &middot;
  <a href="https://github.com/dizchrisctrl/daily-digest">GitHub</a>
</footer>

<script>
function switchTab(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function toggleCard(card) {
  const a = card.querySelector('.qcard-a');
  const h = card.querySelector('.qcard-hint');
  const open = a.style.display === 'block';
  a.style.display = open ? 'none' : 'block';
  h.textContent   = open ? 'tap to reveal' : 'tap to hide';
  card.classList.toggle('open', !open);
}
</script>
</body>
</html>"""


def esc(text):
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def build_story_html(story, color):
    quiz_html = ""
    for q in story.get("quiz", []):
        quiz_html += f"""
      <div class="qcard" onclick="toggleCard(this)">
        <div class="qcard-q">{esc(q.get('q',''))}</div>
        <div class="qcard-a">
          <div class="qcard-ans">{esc(q.get('a',''))}</div>
          <div class="qcard-exp">{esc(q.get('explain',''))}</div>
        </div>
        <div class="qcard-hint">tap to reveal</div>
      </div>"""

    concept_paras = "".join(
        f"<p>{esc(p.strip())}</p>"
        for p in story.get("concept_explained", "").split("\n\n")
        if p.strip()
    )

    return f"""
<article class="story-card">
  <div class="story-header">
    <div class="story-badge" style="background:{color}22;color:{color}">{esc(story.get('source',''))}</div>
    <h2>{esc(story.get('headline',''))}</h2>
    <div class="tldr"><strong>TL;DR:</strong> {esc(story.get('tldr',''))}</div>
  </div>

  <div class="block">
    <div class="blabel">📌 Why It Matters</div>
    <p>{esc(story.get('why_it_matters',''))}</p>
  </div>

  <div class="block concept-block">
    <div class="blabel">🧠 Concept: <span style="color:{color}">{esc(story.get('concept_title',''))}</span></div>
    <div class="concept-text">{concept_paras}</div>
  </div>

  <div class="block">
    <div class="blabel">📊 Visual Diagram</div>
    <pre class="ascii">{esc(story.get('visual_ascii',''))}</pre>
  </div>

  <div class="block opinion-block">
    <div class="blabel">👥 Public Opinion</div>
    <div class="opinion-quote">{esc(story.get('public_opinion',''))}</div>
    <div class="blabel">🔍 Assessment</div>
    <p>{esc(story.get('opinion_assessment',''))}</p>
  </div>

  <div class="block">
    <div class="blabel">❓ Quiz Yourself</div>
    <div class="quiz-grid">{quiz_html}</div>
  </div>

  <div class="block deepdive-block">
    <div class="blabel">💭 Deep Dive</div>
    <p class="deepdive-text">{esc(story.get('deep_dive',''))}</p>
  </div>

  <div class="story-footer">
    <a class="src-link" href="{esc(story.get('source_url','#'))}" target="_blank" rel="noopener">Read original →</a>
    <span class="src-name">{esc(story.get('source',''))}</span>
  </div>
</article>"""


def generate_html(data):
    today    = data["date"]
    ai_html  = "\n".join(build_story_html(s, "#818cf8") for s in data.get("ai_stories", []))
    cy_html  = "\n".join(build_story_html(s, "#34d399") for s in data.get("cyber_stories", []))
    return (HTML_TEMPLATE
            .replace("__DATE__", today)
            .replace("__AI_STORIES__", ai_html)
            .replace("__CYBER_STORIES__", cy_html))


def send_email(data):
    today       = data["date"]
    ai_items    = "".join(f"<li>🤖 <strong>{esc(s['headline'])}</strong> — {esc(s['tldr'])}</li>" for s in data.get("ai_stories", []))
    cyber_items = "".join(f"<li>🔐 <strong>{esc(s['headline'])}</strong> — {esc(s['tldr'])}</li>" for s in data.get("cyber_stories", []))

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
  </div>
  <div style="text-align:center;padding:28px;background:#1a1d2e;border-radius:12px">
    <p style="color:#94a3b8;margin:0 0 20px;font-size:0.93rem">Get concepts, diagrams, quizzes &amp; deep dives in the full interactive digest</p>
    <a href="{PAGES_URL}" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#059669);color:#fff;text-decoration:none;padding:14px 36px;border-radius:10px;font-weight:700;font-size:1rem">Read Full Digest →</a>
  </div>
  <p style="text-align:center;color:#475569;font-size:0.78rem;margin-top:24px">
    Generated with Claude · <a href="https://github.com/dizchrisctrl/daily-digest" style="color:#818cf8;text-decoration:none">daily-digest</a>
  </p>
</div></body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Digest — {today}"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SENDER_EMAIL, GMAIL_APP_PWD)
        smtp.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print("  Email sent")


def save_output(html, data):
    os.makedirs("output", exist_ok=True)
    with open("output/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("output/digest.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("  Saved: output/index.html + output/digest.json")


if __name__ == "__main__":
    print("[ Daily Digest Generator ]")

    print("\n→ Fetching news...")
    ai_articles    = fetch_articles(AI_FEEDS)
    cyber_articles = fetch_articles(CYBER_FEEDS)
    print(f"  AI: {len(ai_articles)} | Cyber: {len(cyber_articles)}")

    print("\n→ Generating with Claude...")
    data = generate_digest_json(ai_articles, cyber_articles)
    print(f"  Stories — AI: {len(data.get('ai_stories',[]))} | Cyber: {len(data.get('cyber_stories',[]))}")

    print("\n→ Building HTML...")
    html = generate_html(data)

    print("\n→ Saving output...")
    save_output(html, data)

    print("\n→ Sending email...")
    send_email(data)

    print("\n✓ Done!")
