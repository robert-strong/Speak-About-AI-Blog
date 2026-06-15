#!/usr/bin/env python3
"""
generate_briefs.py
------------------
Use Claude to brainstorm fresh AI-related blog briefs and append them to the
queue. Supports two backends:

1. REST API (preferred): Database-backed queue via speakabout.ai API
2. Google Sheets (legacy fallback): Append to a Google Sheet

The backend is selected via USE_BLOG_API env var. When set to 'true', the
REST API is used; otherwise falls back to Google Sheets.

USAGE
    python3 generate_briefs.py                # generate 5 briefs (default)
    python3 generate_briefs.py --count 3      # generate 3 briefs
    python3 generate_briefs.py --dry-run      # preview without writing

Required env (loaded from .env / .env.local or set externally):
    ANTHROPIC_API_KEY        — sk-ant-api03-...

For REST API backend (preferred):
    USE_BLOG_API             — set to 'true' to use REST API
    BLOG_API_BASE            — API base URL (default: https://speakabout.ai/api/blog-pipeline)
    BLOG_PIPELINE_API_KEY    — API key for authentication

For Google Sheets backend (legacy fallback):
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

# Only import Google Sheets libraries if needed (they may not be installed)
gspread = None
Credentials = None

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
USE_BLOG_API = os.environ.get("USE_BLOG_API", "").lower() == "true"

# Google Sheets config (legacy)
SHEET_ID = os.environ.get("SHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                                      "service_account.json")
WORKSHEET_NAME = os.environ.get("SHEET_WORKSHEET", "Sheet1")

TEXT_MODEL = "claude-sonnet-4-6"  # Sonnet for higher-quality brainstorming
ANTHROPIC_VERSION = "2023-06-01"

# API client (lazy import)
_api_client = None
_cached_settings = None


def _get_api_client():
    """Lazy-load the API client."""
    global _api_client
    if _api_client is None:
        from api_client import BlogPipelineAPI
        _api_client = BlogPipelineAPI()
    return _api_client


def _get_settings():
    """Fetch settings from the API (cached)."""
    global _cached_settings
    if _cached_settings is None:
        api = _get_api_client()
        _cached_settings = {}
        # Fetch all known settings (keys match admin UI)
        for key in ['briefs_prompt', 'cta_ratio', 'default_brief_count',
                    'brief_length_min', 'brief_length_max',
                    'article_length_min', 'article_length_max',
                    'topic_areas', 'avoid_list', 'search_queries', 'brief_requirements',
                    'enable_web_search', 'max_web_searches']:
            try:
                value = api.get_setting(key)
                if value is not None:
                    _cached_settings[key] = value
            except Exception as e:
                print(f"   Warning: Could not fetch setting '{key}': {e}")
    return _cached_settings


def _load_gspread():
    """Lazy-load Google Sheets libraries."""
    global gspread, Credentials
    if gspread is None:
        import gspread as gs
        from google.oauth2.service_account import Credentials as Creds
        gspread = gs
        Credentials = Creds


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
    _load_gspread()
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


def claude_generate(existing_briefs, count, settings=None):
    """Generate briefs using Claude with optional settings override."""
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    settings = settings or {}
    existing_block = "\n".join(f"- {b}" for b in existing_briefs) or "(none yet — this is the first batch)"

    # Get CTA ratio from settings or use default (0.6)
    cta_ratio = float(settings.get('cta_ratio', '0.6'))
    cta_count = round(count * cta_ratio)
    non_cta_count = count - cta_count
    print(f"   CTA ratio: {cta_ratio} ({cta_count} with CTA, {non_cta_count} without)")

    # Get prompt template from settings or use default
    prompt_template = settings.get('briefs_prompt') or BRIEFS_PROMPT

    # If the custom prompt is empty or whitespace, fall back to default
    if not prompt_template.strip():
        print("   Warning: Custom prompt is empty, using default prompt")
        prompt_template = BRIEFS_PROMPT

    # Build the substitution dictionary
    subs = {
        'count': count,
        'cta_count': cta_count,
        'non_cta_count': non_cta_count,
        'existing_briefs': existing_block,
        'brief_length_min': settings.get('brief_length_min', '100'),
        'brief_length_max': settings.get('brief_length_max', '180'),
        'article_length_min': settings.get('article_length_min', '1500'),
        'article_length_max': settings.get('article_length_max', '1800'),
        'topic_areas': settings.get('topic_areas', ''),
        'avoid_list': settings.get('avoid_list', ''),
        'search_queries': settings.get('search_queries', ''),
        'brief_requirements': settings.get('brief_requirements', ''),
    }

    # Try to format the prompt, falling back to default if there's an error
    try:
        prompt = prompt_template.format(**subs)
    except KeyError as e:
        print(f"   Warning: Prompt template has unknown variable {e}, using default prompt")
        prompt = BRIEFS_PROMPT.format(**subs)

    # Determine whether to enable web search and how many searches to allow
    enable_web_search = settings.get('enable_web_search', 'true').lower() in ('true', '1', 'yes')
    max_web_searches = int(settings.get('max_web_searches', '5'))
    print(f"   Web search: {'enabled' if enable_web_search else 'disabled'} (max {max_web_searches})")

    # Build request payload
    request_json = {
        "model": TEXT_MODEL,
        "max_tokens": 8192,
        "temperature": 0.85,
        "messages": [{"role": "user", "content": prompt}],
    }

    # Only add web search tool if enabled
    if enable_web_search and max_web_searches > 0:
        request_json["tools"] = [{
            # Server-side web search; Anthropic runs the searches
            # transparently and returns them as content blocks alongside text.
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_web_searches,
        }]

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=request_json,
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

    # Debug: show what we got before filtering
    print(f"   Raw parsed array has {len(briefs)} items")
    for i, item in enumerate(briefs):
        item_type = type(item).__name__
        preview = str(item)[:100] if item else "(empty)"
        print(f"      [{i}] ({item_type}): {preview}...")

    # Handle both string briefs and object briefs
    # If Claude returned objects (dicts), convert them to strings
    processed = []
    for idx, item in enumerate(briefs):
        print(f"   Processing item {idx}: type={type(item).__name__}")
        if isinstance(item, str) and item.strip():
            processed.append(item.strip())
            print(f"      -> Added as string ({len(item)} chars)")
        elif isinstance(item, dict):
            print(f"      -> Dict with keys: {list(item.keys())}")
            # Convert dict to a formatted brief string
            # Try common field names for the brief content (in priority order)
            brief_text = None

            # Check for content-like fields first
            content_keys = ['brief', 'content', 'description', 'summary', 'text', 'body']
            for key in content_keys:
                if key in item and item[key]:
                    brief_text = str(item[key]).strip()
                    print(f"      -> Found content in '{key}' field ({len(brief_text)} chars)")
                    break

            # If no content field found, format ALL fields as a structured brief
            # This handles cases where Claude returns structured data like:
            # {"title": "...", "audience": "...", "angle": "...", "seo_targets": [...]}
            if not brief_text:
                print(f"      -> No content field found, formatting all {len(item)} fields as structured brief")
                parts = []

                # Define preferred order for common brief fields
                preferred_order = ['title', 'audience', 'angle', 'thesis', 'sections', 'subtopics',
                                   'examples', 'case_studies', 'companies', 'seo_keywords',
                                   'seo_targets', 'keywords', 'cta', 'cta_hook', 'close', 'notes']

                # Process fields in preferred order first
                seen_keys = set()
                for key in preferred_order:
                    if key in item and item[key]:
                        seen_keys.add(key)
                        value = item[key]
                        # Format arrays as comma-separated
                        if isinstance(value, list):
                            value = ", ".join(str(v) for v in value)
                        # Capitalize key nicely
                        nice_key = key.replace('_', ' ').title()
                        parts.append(f"{nice_key}: {value}")

                # Then add any remaining keys not in preferred order
                for key, value in item.items():
                    if key not in seen_keys and value:
                        if isinstance(value, list):
                            value = ", ".join(str(v) for v in value)
                        nice_key = key.replace('_', ' ').title()
                        parts.append(f"{nice_key}: {value}")

                if parts:
                    # Use newlines for readability when there are many fields
                    if len(parts) > 3:
                        brief_text = "\n".join(parts)
                    else:
                        brief_text = " | ".join(parts)
                    print(f"      -> Formatted brief has {len(parts)} parts, {len(brief_text)} chars")
                else:
                    print(f"      -> WARNING: Dict has no usable fields")

            if brief_text and brief_text.strip():
                processed.append(brief_text.strip())
                print(f"      -> SUCCESS: Added brief ({len(brief_text)} chars)")
            else:
                print(f"      -> FAILED: brief_text is empty or whitespace")
        else:
            print(f"      -> Skipped (not string or dict, or empty string)")

    print(f"   Total processed: {len(processed)} briefs")
    return processed


REQUIRED_HEADERS = ("Status", "Brief")


def validate_headers(headers):
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        sys.exit(
            f"Sheet is missing required header column(s): {missing}.\n"
            f"  Required: {list(REQUIRED_HEADERS)}\n"
            f"  Found in row 1: {headers}\n"
            "Headers are exact, case-sensitive matches with no surrounding whitespace."
        )


def append_to_sheet(ws, headers, briefs):
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
    p = argparse.ArgumentParser(description="Generate AI blog briefs and append to queue.")
    p.add_argument("--count", type=int, default=5,
                   help="Number of briefs to generate (default 5; covers 5-Monday months).")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview briefs without writing to the queue.")
    args = p.parse_args()

    # Determine which backend to use
    if USE_BLOG_API:
        print("Using REST API backend...")
        api = _get_api_client()

        # Fetch settings from API
        print("Fetching settings from API...")
        settings = _get_settings()
        if settings:
            print(f"   Loaded {len(settings)} setting(s): {', '.join(settings.keys())}")
        else:
            print("   No custom settings found, using defaults.")

        # Get existing briefs via API
        print("Fetching existing briefs from API...")
        existing = api.get_existing_briefs(limit=30)
        print(f"Found {len(existing)} existing brief(s) (using last 30 for de-dup context).")

        print(f"Asking Claude ({TEXT_MODEL}) for {args.count} new briefs (with web search grounding)...")
        briefs = claude_generate(existing, args.count, settings=settings)
        print(f"Generated {len(briefs)} briefs.\n")

        if not briefs:
            print("WARNING: No briefs were generated. Check if the prompt template is valid.")
            print("The prompt may have formatting issues or Claude may have returned invalid JSON.")
            sys.exit(1)

        for i, b in enumerate(briefs, 1):
            print(f"--- Brief {i} ({len(b)} chars) ---")
            print(b)
            print()

        if args.dry_run:
            print("[dry-run] Not writing to queue.")
            return

        # Create briefs via API
        created = api.create_briefs(briefs)
        print(f"Created {len(created)} new queued briefs via API.")
        for item in created:
            print(f"   - ID {item['id']}: {item['brief'][:60]}...")

    else:
        # Legacy Google Sheets backend
        print("Using Google Sheets backend (legacy)...")
        if not SHEET_ID:
            sys.exit("SHEET_ID env var required (or set USE_BLOG_API=true to use REST API).")
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            sys.exit(f"Service account file not found at {SERVICE_ACCOUNT_FILE}.")

        print(f"Opening sheet {SHEET_ID} (worksheet: {WORKSHEET_NAME})...")
        ws = open_sheet()
        all_values = ws.get_all_values()
        if not all_values:
            sys.exit("Sheet is empty - needs at least a header row.")
        headers = all_values[0]
        validate_headers(headers)

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
