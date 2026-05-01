#!/usr/bin/env python3
"""
generate_briefs.py
------------------
Use Claude to brainstorm fresh AI-related blog briefs and append them to the
queue sheet. Each brief lands as a new row with Status=Queued and Brief
populated; the existing pipeline (draft_articles.py + from_sheet.py) takes
it from there.

Briefs are written to be detailed, reference real recent AI developments,
target SEO keywords, and include a natural CTA hook toward Speak About AI's
keynote speaker roster.

USAGE
    python3 generate_briefs.py                # generate 5 briefs (default)
    python3 generate_briefs.py --count 3      # generate 3 briefs
    python3 generate_briefs.py --dry-run      # preview without writing

Required env (loaded from .env / .env.local or set externally):
    ANTHROPIC_API_KEY        — sk-ant-api03-...
    SHEET_ID                 — Google Sheet ID
    GOOGLE_SERVICE_ACCOUNT_FILE — path to service_account.json (default: ./service_account.json)
    SHEET_WORKSHEET          — worksheet name (default: Sheet1)
"""

import argparse
import json
import os
import re
import sys

# Force UTF-8 stdout/stderr on Windows so smart quotes & em-dashes render.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)
except ImportError:
    pass

import requests
import gspread
from google.oauth2.service_account import Credentials


ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
SHEET_ID = os.environ.get("SHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                                      "service_account.json")
WORKSHEET_NAME = os.environ.get("SHEET_WORKSHEET", "Sheet1")
TEXT_MODEL = "claude-sonnet-4-6"  # Sonnet for higher-quality brainstorming
ANTHROPIC_VERSION = "2023-06-01"


BRIEFS_PROMPT = """You are generating fresh blog post briefs for Speak About AI (https://speakabout.ai), a premier AI keynote speakers bureau. The audience for the resulting articles: event planners, corporate marketers, sales/revenue/HR/L&D leaders, and executives who book speakers for corporate events. The articles should also rank in Google for AI-related search queries and pull organic traffic to speakabout.ai.

GROUND TRUTH IN CURRENT EVENTS — IMPORTANT
Before writing any briefs, USE THE web_search TOOL 2-4 times to find recent (last 30-60 days) AI developments. Search for things like:
- "enterprise AI deployment 2026"
- recent AI product launches by major labs (OpenAI, Anthropic, Google, etc.)
- AI regulation news and policy shifts
- industry-specific AI applications (healthcare, finance, sales, manufacturing, etc.)
- notable AI case studies, controversies, or research findings

Use the actual headlines, companies, products, and findings you discover as the grounding for your briefs. Do NOT rely solely on training knowledge that may be months out of date. Each brief should reference at least one specific real development you found in your searches.

Generate {count} distinct, high-quality briefs. Each brief must:

REQUIREMENTS
1. Cover a different angle on AI today or the near future (next 6-12 months). Vary the topic areas across briefs — no two briefs should target the same audience or sub-topic.
2. Reference at least one specific real-world example, recent news event, company, product launch, or research finding. Use your knowledge of recent AI developments — be accurate; don't fabricate specifics or invent quotes.
3. Provide enough specificity that a writer can produce a 1500-1800 word article from the brief alone. Each brief must explicitly name:
   - Target audience for the article
   - Specific angle or thesis (what makes this article's take different from generic AI content)
   - 3-5 sub-topics or sections to cover
   - 2-3 concrete examples, case studies, or companies to reference
4. Identify 2-3 SEO target keyword phrases (long-tail) the article should rank for.
5. End with EITHER a CTA hook tied to booking AI keynote speakers OR a substantive non-sales close — see CTA RATIO section below. When using a CTA hook, the closing beat should feel natural — sample phrasings: "the kind of insight that lands harder when delivered live by a keynote speaker," "for organizations ready to align their teams, an AI keynote can accelerate the conversation," "to bring this perspective to your next event, browse our AI speaker roster." When NOT using a CTA hook, end with a substantive editorial close — a forward-looking question, a takeaway implication, or a thought-provoking observation about the topic — and DO NOT mention keynote speakers, Speak About AI, or any sales beat.

CTA RATIO — IMPORTANT
Of these {count} briefs, EXACTLY {cta_count} should include the speaker-bureau CTA hook (per requirement #5). The remaining {non_cta_count} briefs should end with a substantive non-sales editorial close — no mention of keynote speakers, no funnel toward Speak About AI's roster, no sales beat at all. Just smart commentary that lands on its own.

You decide which briefs get the CTA based on topic fit. Some topics naturally invite a "bring this conversation to your event" close (e.g., AI strategy for executives, change management, internal alignment); others read better as straight editorial without the sales beat (e.g., regulatory analysis, breaking news commentary, technical deep-dives). Distribute the {cta_count} CTAs across the batch wherever they feel most natural.

LENGTH: Each brief 100-180 words. Detailed enough to be useful; not so long it becomes the article itself.

TOPIC AREAS TO ROTATE ACROSS (pick a different one per brief):
- AI in specific industries (healthcare, finance, manufacturing, retail, legal, education, real estate, media, hospitality, logistics)
- Enterprise AI deployment, governance, organizational change management
- AI for sales, marketing, customer service, HR/recruiting
- Generative AI / AI agents / multi-modal AI applications
- AI impact on jobs, hiring, talent strategy, reskilling
- AI security, deepfakes, misinformation, brand protection
- AI strategy and decision-making for executives and boards
- AI in events, conferences, B2B marketing, demand generation
- Recent breakthroughs or product/regulatory shifts
- AI economics: compute costs, infrastructure, ROI, build-vs-buy
- Practical AI adoption patterns: what's working vs. what's stalling

AVOID
- Duplicating angles from the existing briefs listed below
- Vague AI-thought-leadership generalities without concrete specifics
- Generic "what is AI" explainers
- Overplayed framings — bring a fresh contrarian or specific angle (e.g., not "ChatGPT for business" but "Why ChatGPT-only deployments stall in enterprise: the integration gap")

EXISTING BRIEFS (do not duplicate these angles):
{existing_briefs}

OUTPUT FORMAT
Reply with ONLY a JSON array of {count} strings. Each string is a single brief. No preamble, no markdown code fences, no explanation. The output must be valid JSON parseable by json.loads(). Use double quotes inside briefs by escaping them as \\".

Example output structure:
[
  "Audience: ... Angle: ... Cover: ... Examples: ... SEO targets: ... CTA hook: ...",
  "Audience: ... etc."
]
"""


def open_sheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    book = gc.open_by_key(SHEET_ID)
    try:
        return book.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        return book.get_worksheet(0)


def get_existing_briefs(ws, headers, limit=30):
    """Pull existing briefs (most recent N) so Claude knows what NOT to duplicate."""
    if "Brief" not in headers:
        return []
    brief_idx = headers.index("Brief")
    all_values = ws.get_all_values()
    existing = []
    for row in all_values[1:]:
        if len(row) > brief_idx and row[brief_idx].strip():
            existing.append(row[brief_idx].strip())
    return existing[-limit:]


def claude_generate(existing_briefs, count):
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    existing_block = "\n".join(f"- {b}" for b in existing_briefs) or "(none yet — this is the first batch)"
    # 60% of briefs get a CTA hook; 40% end with a non-sales editorial close.
    cta_count = round(count * 0.6)
    non_cta_count = count - cta_count
    print(f"   Target CTA distribution: {cta_count} with CTA, {non_cta_count} without")
    prompt = BRIEFS_PROMPT.format(
        count=count,
        cta_count=cta_count,
        non_cta_count=non_cta_count,
        existing_briefs=existing_block,
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": TEXT_MODEL,
            "max_tokens": 8192,
            "temperature": 0.85,
            "tools": [{
                # Server-side web search; Anthropic runs the searches
                # transparently and returns them as content blocks alongside text.
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=240,
    )
    if not r.ok:
        sys.exit(f"Claude error {r.status_code}: {r.text[:500]}")
    response = r.json()
    blocks = response.get("content", [])

    # Surface searches Claude performed so the run log is auditable.
    searches = [
        b.get("input", {}).get("query")
        for b in blocks
        if b.get("type") == "server_tool_use" and b.get("name") == "web_search"
    ]
    if searches:
        print(f"Claude performed {len(searches)} web search(es):")
        for q in searches:
            print(f"   - {q!r}")

    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    # Strip markdown fences if Claude added them despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        briefs = json.loads(text)
    except json.JSONDecodeError as e:
        # Try to salvage: find the first '[' and last ']' and parse that slice
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                briefs = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                sys.exit(f"Claude returned non-JSON output: {e}\nFirst 500 chars: {text[:500]}")
        else:
            sys.exit(f"Claude returned non-JSON output: {e}\nFirst 500 chars: {text[:500]}")

    if not isinstance(briefs, list):
        sys.exit(f"Expected JSON array; got {type(briefs).__name__}")
    briefs = [b.strip() for b in briefs if isinstance(b, str) and b.strip()]
    return briefs


def append_to_sheet(ws, headers, briefs):
    if "Status" not in headers or "Brief" not in headers:
        sys.exit("Sheet must have 'Status' and 'Brief' header columns.")
    status_idx = headers.index("Status")
    brief_idx = headers.index("Brief")
    rows = []
    for b in briefs:
        row = [""] * len(headers)
        row[status_idx] = "Queued"
        row[brief_idx] = b
        rows.append(row)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


def main():
    p = argparse.ArgumentParser(description="Generate AI blog briefs and append to queue sheet.")
    p.add_argument("--count", type=int, default=5,
                   help="Number of briefs to generate (default 5; covers 5-Monday months).")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview briefs without writing to the sheet.")
    args = p.parse_args()

    if not SHEET_ID:
        sys.exit("SHEET_ID env var required.")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        sys.exit(f"Service account file not found at {SERVICE_ACCOUNT_FILE}.")

    print(f"Opening sheet {SHEET_ID} (worksheet: {WORKSHEET_NAME})...")
    ws = open_sheet()
    all_values = ws.get_all_values()
    if not all_values:
        sys.exit("Sheet is empty - needs at least a header row.")
    headers = all_values[0]

    existing = get_existing_briefs(ws, headers)
    print(f"Found {len(existing)} existing brief(s) in the sheet (using last 30 for de-dup context).")

    print(f"Asking Claude ({TEXT_MODEL}) for {args.count} new briefs (with web search grounding)...")
    briefs = claude_generate(existing, args.count)
    print(f"Generated {len(briefs)} briefs.\n")

    for i, b in enumerate(briefs, 1):
        print(f"--- Brief {i} ({len(b)} chars) ---")
        print(b)
        print()

    if args.dry_run:
        print("[dry-run] Not writing to sheet.")
        return

    n = append_to_sheet(ws, headers, briefs)
    print(f"Appended {n} new Queued briefs to '{ws.title}'.")


if __name__ == "__main__":
    main()
