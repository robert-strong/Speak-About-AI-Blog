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

Optional, for de-duplicating against articles already in Contentful:
    CONTENTFUL_CMA_TOKEN     — CFPAT-... (same token the publish step uses)
    CONTENTFUL_SPACE_ID      — Contentful space ID
    CONTENTFUL_ENVIRONMENT   — default 'master'
    SKIP_CONTENTFUL_HISTORY  — set 'true' to skip the catalogue check

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
import time

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

try:
    from contentful_history import (fetch_published_topics, format_history_block,
                                    closest_matches)
except ImportError:  # keep the script runnable if the module is missing
    fetch_published_topics = format_history_block = closest_matches = None

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
                    'enable_web_search', 'max_web_searches',
                    'contentful_dedup', 'contentful_dedup_drop']:
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
- Repeating the subject of any article already published on the site (listed below), even under a new title or with a different hook
- Vague AI-thought-leadership generalities without concrete specifics
- Generic "what is AI" explainers
- Overplayed framings — bring a fresh contrarian or specific angle (e.g., not "ChatGPT for business" but "Why ChatGPT-only deployments stall in enterprise: the integration gap")

EXISTING BRIEFS (do not duplicate these angles):
{existing_briefs}

ARTICLES ALREADY PUBLISHED ON SPEAKABOUT.AI (do not repeat these subjects; a new brief must take a clearly different subject, or a genuinely new angle that a reader of the existing article would still need):
{published_topics}

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


def _stream_claude_message(request_json):
    """POST /v1/messages with stream=true and reassemble the final message.

    Long generations over a single non-streaming HTTP request get the
    connection dropped by the API after a few minutes; streaming keeps the
    connection alive. Returns (status_code, error_text, message) where
    message mimics the non-streaming response shape: {"content": [...],
    "stop_reason": ...}. Raises requests.ConnectionError on a mid-stream
    error event so the caller's retry loop handles it.
    """
    payload = dict(request_json, stream=True)
    with requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=payload,
        stream=True,
        timeout=(30, 120),  # (connect, per-chunk read); pings keep it alive
    ) as r:
        if not r.ok:
            return r.status_code, r.text[:500], None

        blocks = []
        stop_reason = None
        partial_json = ""
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                event = json.loads(raw[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "content_block_start":
                blocks.append(dict(event.get("content_block") or {}))
                partial_json = ""
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and blocks:
                    blocks[-1]["text"] = blocks[-1].get("text", "") + delta.get("text", "")
                elif delta.get("type") == "input_json_delta":
                    partial_json += delta.get("partial_json", "")
            elif etype == "content_block_stop":
                # Tool-use inputs (e.g. web_search queries) arrive as
                # partial JSON deltas; parse once the block closes.
                if blocks and partial_json and blocks[-1].get("type") in ("server_tool_use", "tool_use"):
                    try:
                        blocks[-1]["input"] = json.loads(partial_json)
                    except json.JSONDecodeError:
                        pass
                partial_json = ""
            elif etype == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
            elif etype == "error":
                raise requests.ConnectionError(
                    f"API stream error: {event.get('error')}")

        return 200, None, {"content": blocks, "stop_reason": stop_reason}


def _looks_like_briefs(value):
    """A briefs array is a non-empty list of strings and/or objects — this
    rejects decoys like citation markers ([1]) or nested keyword lists that
    happen to parse."""
    return (isinstance(value, list) and value
            and all(isinstance(x, (str, dict)) for x in value))


def _repair_truncated_array(fragment):
    """Given text starting at '[' that doesn't parse (reply cut off at
    max_tokens), trim back to the last complete element and close the array."""
    for m in reversed(list(re.finditer(r'[}"]', fragment))):
        snippet = fragment[:m.end()].rstrip().rstrip(",") + "]"
        try:
            value = json.loads(snippet)
        except json.JSONDecodeError:
            continue
        if _looks_like_briefs(value):
            return value
    return None


def _extract_briefs_array(text):
    """Extract a JSON array from Claude's reply, tolerating prose preambles,
    markdown code fences, and output truncated at the token limit."""
    decoder = json.JSONDecoder()

    # Prefer content inside a fenced code block if one exists (the fence may
    # be unterminated when the reply was cut off at max_tokens).
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)

    for cand in candidates:
        idx = cand.find("[")
        while idx != -1:
            try:
                value, _ = decoder.raw_decode(cand, idx)
                if _looks_like_briefs(value):
                    return value
            except json.JSONDecodeError:
                # Attempt truncation repair here, before falling through to a
                # later '[' — otherwise a nested array inside the truncated
                # outer array (e.g. a keywords list) would win incorrectly.
                repaired = _repair_truncated_array(cand[idx:])
                if repaired is not None:
                    return repaired
            idx = cand.find("[", idx + 1)
    return None


def claude_generate(existing_briefs, count, settings=None, published_topics=None):
    """Generate briefs using Claude with optional settings override.

    published_topics is the Contentful catalogue from contentful_history
    (list of dicts). It is rendered into the prompt so Claude can steer clear
    of subjects the site already covers.
    """
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    settings = settings or {}
    existing_block = "\n".join(f"- {b}" for b in existing_briefs) or "(none yet — this is the first batch)"
    if published_topics and format_history_block:
        published_block = format_history_block(published_topics)
    else:
        published_block = "(catalogue unavailable for this run)"

    # Get CTA ratio from settings or use default (0.6)
    try:
        cta_ratio = float(settings.get('cta_ratio') or 0.6)
    except (TypeError, ValueError):
        print(f"   Warning: Invalid cta_ratio setting {settings.get('cta_ratio')!r}, using 0.6")
        cta_ratio = 0.6
    cta_count = round(count * cta_ratio)
    non_cta_count = count - cta_count
    print(f"   CTA ratio: {cta_ratio} ({cta_count} with CTA, {non_cta_count} without)")

    # Get prompt template from settings or use default
    prompt_template = settings.get('briefs_prompt') or BRIEFS_PROMPT

    # If the custom prompt is empty, whitespace, or not text, fall back to default
    if not isinstance(prompt_template, str) or not prompt_template.strip():
        print("   Warning: Custom prompt is empty or not text, using default prompt")
        prompt_template = BRIEFS_PROMPT

    # A custom prompt saved before the catalogue existed has no
    # {published_topics} slot. Fold the catalogue into the existing-briefs
    # block so it still reaches Claude through the slot every prompt has.
    if '{published_topics}' not in prompt_template and published_topics:
        existing_block += ("\n\nARTICLES ALREADY PUBLISHED ON SPEAKABOUT.AI "
                           "(do not repeat these subjects):\n" + published_block)

    # Build the substitution dictionary
    subs = {
        'count': count,
        'cta_count': cta_count,
        'non_cta_count': non_cta_count,
        'existing_briefs': existing_block,
        'published_topics': published_block,
        'brief_length_min': settings.get('brief_length_min', '100'),
        'brief_length_max': settings.get('brief_length_max', '180'),
        'article_length_min': settings.get('article_length_min', '1500'),
        'article_length_max': settings.get('article_length_max', '1800'),
        'topic_areas': settings.get('topic_areas', ''),
        'avoid_list': settings.get('avoid_list', ''),
        'search_queries': settings.get('search_queries', ''),
        'brief_requirements': settings.get('brief_requirements', ''),
    }

    # Try to format the prompt, falling back to default if there's an error.
    # KeyError = unknown {variable}; ValueError/IndexError = stray unescaped
    # brace in a custom prompt (e.g. literal JSON in the template).
    try:
        prompt = prompt_template.format(**subs)
    except (KeyError, IndexError, ValueError) as e:
        print(f"   Warning: Prompt template failed to format ({type(e).__name__}: {e}), using default prompt")
        prompt = BRIEFS_PROMPT.format(**subs)

    # Determine whether to enable web search and how many searches to allow
    enable_web_search = str(settings.get('enable_web_search', 'true')).lower() in ('true', '1', 'yes')
    try:
        max_web_searches = int(settings.get('max_web_searches') or 5)
    except (TypeError, ValueError):
        print(f"   Warning: Invalid max_web_searches setting {settings.get('max_web_searches')!r}, using 5")
        max_web_searches = 5
    print(f"   Web search: {'enabled' if enable_web_search else 'disabled'} (max {max_web_searches})")

    # Build request payload
    request_json = {
        "model": TEXT_MODEL,
        "max_tokens": 16384,
        "temperature": 0.85,
        "system": ("Your final reply must be ONLY the requested JSON array — "
                   "no prose before or after it and no markdown code fences. "
                   "It must parse with json.loads()."),
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

    # Generation with web search + 16k max_tokens runs longer than the API
    # allows a single non-streaming HTTP response to stay open, so stream
    # the reply. Retry on timeouts and transient errors so one hiccup
    # doesn't kill the monthly run.
    attempts = 3
    response = None
    for attempt in range(1, attempts + 1):
        try:
            status, err_text, response = _stream_claude_message(request_json)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == attempts:
                sys.exit(f"Claude request failed after {attempts} attempts: {e}")
            print(f"   Attempt {attempt}/{attempts} failed ({type(e).__name__}), retrying...")
            time.sleep(15)
            continue
        if status in (429, 500, 502, 503, 504, 529) and attempt < attempts:
            print(f"   Attempt {attempt}/{attempts} got HTTP {status}, retrying...")
            time.sleep(30)
            continue
        if status != 200:
            sys.exit(f"Claude error {status}: {err_text}")
        break
    if response is None:
        sys.exit(f"Claude request did not succeed after {attempts} attempts.")
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

    if response.get("stop_reason") == "max_tokens":
        print("   Warning: reply hit the max_tokens limit and was truncated; "
              "salvaging the complete briefs from the partial output.")

    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    briefs = _extract_briefs_array(text)
    if briefs is None:
        sys.exit(f"Claude returned non-JSON output.\nFirst 500 chars: {text[:500]}")

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
    print("=" * 50, flush=True)
    print("STARTING PROCESSING LOOP", flush=True)
    print(f"briefs list has {len(briefs)} items", flush=True)
    print("=" * 50, flush=True)
    sys.stdout.flush()

    processed = []

    for idx, item in enumerate(briefs):
        print(f"--- LOOP ITERATION {idx} ---", flush=True)
        item_type = type(item).__name__
        print(f"Item type: {item_type}", flush=True)

        try:
            if isinstance(item, str):
                if item.strip():
                    processed.append(item.strip())
                    print(f"Added string brief ({len(item)} chars)", flush=True)
                else:
                    print("Skipped empty string", flush=True)

            elif isinstance(item, dict):
                print(f"Processing dict with keys: {list(item.keys())}", flush=True)

                # Simple approach: just format all dict fields
                parts = []
                for key, value in item.items():
                    if value:
                        if isinstance(value, list):
                            value = ", ".join(str(v) for v in value)
                        nice_key = key.replace('_', ' ').title()
                        parts.append(f"{nice_key}: {value}")
                        print(f"  Added field: {nice_key}", flush=True)

                if parts:
                    brief_text = "\n".join(parts)
                    processed.append(brief_text)
                    print(f"SUCCESS: Created brief with {len(parts)} fields, {len(brief_text)} chars", flush=True)
                else:
                    # Fallback: JSON stringify
                    brief_text = json.dumps(item, ensure_ascii=False)
                    processed.append(brief_text)
                    print(f"Fallback: JSON stringified ({len(brief_text)} chars)", flush=True)
            else:
                print(f"Skipped unknown type: {item_type}", flush=True)

        except Exception as e:
            print(f"EXCEPTION in processing: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            # Try to salvage
            try:
                fallback = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                processed.append(fallback)
                print(f"Salvaged via exception handler ({len(fallback)} chars)", flush=True)
            except Exception as e2:
                print(f"Salvage also failed: {e2}", flush=True)

    print("=" * 50, flush=True)
    print(f"PROCESSING COMPLETE: {len(processed)} briefs", flush=True)
    print("=" * 50, flush=True)
    return processed


def _setting_flag(settings, key, default=True):
    """Read a true/false setting saved from the admin UI."""
    value = settings.get(key) if settings else None
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def load_contentful_history(settings=None):
    """Fetch the blog catalogue from Contentful, or [] if disabled/unavailable.

    Disable with the admin setting contentful_dedup=false or the env var
    SKIP_CONTENTFUL_HISTORY=true. A fetch failure never aborts the run; the
    generator just falls back to queue-only de-duplication.
    """
    if os.environ.get("SKIP_CONTENTFUL_HISTORY", "").lower() in ("true", "1", "yes"):
        print("Contentful history check disabled via SKIP_CONTENTFUL_HISTORY.")
        return []
    if not _setting_flag(settings, 'contentful_dedup', True):
        print("Contentful history check disabled via contentful_dedup setting.")
        return []
    if fetch_published_topics is None:
        print("   Warning: contentful_history module not available; skipping catalogue check.")
        return []
    has_token = os.environ.get("CONTENTFUL_CMA_TOKEN") or os.environ.get("CONTENTFUL_MANAGEMENT_TOKEN")
    if not has_token or not os.environ.get("CONTENTFUL_SPACE_ID"):
        print("   Warning: CONTENTFUL_CMA_TOKEN / CONTENTFUL_SPACE_ID not set; "
              "briefs will only be de-duplicated against the queue, not the published site.")
        return []
    print("Fetching published article catalogue from Contentful...")
    try:
        topics = fetch_published_topics()
    except Exception as e:
        print(f"   Warning: Could not fetch Contentful catalogue ({type(e).__name__}: {e}); "
              "continuing without it.")
        return []
    live = sum(1 for t in topics if t["live"])
    print(f"Found {len(topics)} article(s) in Contentful ({live} published, "
          f"{len(topics) - live} awaiting approval).")
    return topics


def _claude_json_request(request_json, attempts=3):
    """Run a Claude request with retries and return the reply text, or None."""
    for attempt in range(1, attempts + 1):
        try:
            status, err_text, response = _stream_claude_message(request_json)
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"   Attempt {attempt}/{attempts} failed ({type(e).__name__})")
            if attempt < attempts:
                time.sleep(15)
            continue
        if status in (429, 500, 502, 503, 504, 529) and attempt < attempts:
            print(f"   Attempt {attempt}/{attempts} got HTTP {status}, retrying...")
            time.sleep(30)
            continue
        if status != 200:
            print(f"   Claude error {status}: {err_text}")
            return None
        blocks = response.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    return None


def _parse_json_array(text):
    """Pull the first JSON array out of a reply, tolerating fences and preambles."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def judge_briefs_against_history(briefs, topics):
    """Ask Claude which candidate briefs repeat an article already on the site.

    Returns a list (one entry per brief, same order) of dicts:
        {"verdict": "duplicate" | "overlap" | "distinct",
         "matches": [slug, ...], "reason": str}
    Returns None if the judge could not run, so the caller keeps every brief.
    """
    if not briefs or not topics or not ANTHROPIC_KEY:
        return None

    catalogue = "\n".join(
        f"{i + 1}. {t['title']} (/{t['slug']})" + (f" — {t['excerpt'][:200]}" if t['excerpt'] else "")
        for i, t in enumerate(topics)
    )
    candidates = "\n\n".join(f"BRIEF {i + 1}:\n{b}" for i, b in enumerate(briefs))
    prompt = f"""You are the editor of the Speak About AI blog. Below is every article already on the site, followed by candidate briefs for new articles. For EACH candidate, decide whether the site already covers its subject.

Verdicts:
- "duplicate": the core subject and angle are already covered by an existing article. A reader of that article would learn nothing new. Different wording or a new hook does not make it distinct.
- "overlap": shares a topic area with an existing article but takes a clearly different angle, audience, or question that the existing piece does not answer.
- "distinct": no existing article covers this subject.

EXISTING ARTICLES:
{catalogue}

CANDIDATE BRIEFS:
{candidates}

Reply with ONLY a JSON array with exactly {len(briefs)} objects, in brief order, each shaped as:
{{"brief": <number>, "verdict": "duplicate" | "overlap" | "distinct", "matches": ["<slug of the closest existing article, if any>"], "reason": "<one sentence>"}}
No preamble, no markdown fences."""

    request_json = {
        "model": TEXT_MODEL,
        "max_tokens": 4096,
        "temperature": 0,
        "system": "Reply with ONLY the requested JSON array. It must parse with json.loads().",
        "messages": [{"role": "user", "content": prompt}],
    }
    text = _claude_json_request(request_json)
    parsed = _parse_json_array(text)
    if not isinstance(parsed, list):
        print("   Warning: duplicate judge returned unparseable output; keeping all briefs.")
        return None

    by_number = {}
    for item in parsed:
        if isinstance(item, dict):
            try:
                by_number[int(item.get("brief"))] = item
            except (TypeError, ValueError):
                pass
    verdicts = []
    for i in range(len(briefs)):
        item = by_number.get(i + 1)
        if item is None:
            item = parsed[i] if i < len(parsed) and isinstance(parsed[i], dict) else {}
        verdict = str(item.get("verdict", "distinct")).strip().lower()
        if verdict not in ("duplicate", "overlap", "distinct"):
            verdict = "distinct"
        matches = item.get("matches") or []
        if isinstance(matches, str):
            matches = [matches]
        verdicts.append({
            "verdict": verdict,
            "matches": [str(m) for m in matches if m],
            "reason": str(item.get("reason", "")).strip(),
        })
    return verdicts


def _print_verdict(v):
    label = v["verdict"].upper()
    match = f" -> {', '.join(v['matches'])}" if v["matches"] else ""
    print(f"   [{label}]{match} {v['reason'][:160]}")


def screen_briefs_against_history(briefs, topics, existing, count, settings=None):
    """Drop briefs that repeat published subjects, then top up once.

    1. Print the closest published titles for each brief (lexical, for the log).
    2. Ask Claude to judge each brief against the catalogue and drop the
       "duplicate" ones (unless contentful_dedup_drop=false, which only warns).
    3. If briefs were dropped, ask for replacements once, with the dropped
       briefs added to the avoid list, and judge those too.
    """
    if not briefs or not topics:
        return briefs

    print("Checking briefs against the published catalogue...")
    if closest_matches:
        for i, b in enumerate(briefs, 1):
            near = closest_matches(b, topics, top_n=2, min_score=0.34)
            if near:
                hits = "; ".join(f"{t['title']} ({score:.0%})" for score, t in near)
                print(f"   Brief {i} shares vocabulary with: {hits}")

    drop = _setting_flag(settings, 'contentful_dedup_drop', True)
    verdicts = judge_briefs_against_history(briefs, topics)
    if verdicts is None:
        return briefs

    kept, dropped = [], []
    for b, v in zip(briefs, verdicts):
        _print_verdict(v)
        if v["verdict"] == "duplicate" and drop:
            dropped.append(b)
        else:
            kept.append(b)

    if not dropped:
        print("   No duplicates of published articles found.")
        return kept
    print(f"   Dropped {len(dropped)} brief(s) that repeat published subjects.")

    shortfall = count - len(kept)
    if shortfall <= 0:
        return kept

    print(f"Requesting {shortfall} replacement brief(s)...")
    avoid = list(existing) + kept + dropped
    try:
        extra = claude_generate(avoid, shortfall, settings=settings, published_topics=topics)
    except SystemExit as e:
        print(f"   Warning: replacement generation failed ({e}); continuing with {len(kept)} brief(s).")
        return kept
    extra_verdicts = judge_briefs_against_history(extra, topics)
    if extra_verdicts is None:
        extra_verdicts = [{"verdict": "distinct", "matches": [], "reason": ""} for _ in extra]
    for b, v in zip(extra, extra_verdicts):
        _print_verdict(v)
        if v["verdict"] == "duplicate" and drop:
            print("   Replacement also duplicates a published subject; discarding it.")
        else:
            kept.append(b)
    return kept[:count]


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

        # Published catalogue from Contentful, so new briefs avoid old subjects
        topics = load_contentful_history(settings)

        print(f"Asking Claude ({TEXT_MODEL}) for {args.count} new briefs (with web search grounding)...")
        briefs = claude_generate(existing, args.count, settings=settings, published_topics=topics)
        print(f"Generated {len(briefs)} briefs.\n")

        if not briefs:
            print("WARNING: No briefs were generated. Check if the prompt template is valid.")
            print("The prompt may have formatting issues or Claude may have returned invalid JSON.")
            sys.exit(1)

        briefs = screen_briefs_against_history(briefs, topics, existing, args.count, settings=settings)
        if not briefs:
            print("WARNING: Every generated brief repeated a published subject; nothing to queue.")
            sys.exit(1)
        print()

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

        topics = load_contentful_history()

        print(f"Asking Claude ({TEXT_MODEL}) for {args.count} new briefs (with web search grounding)...")
        briefs = claude_generate(existing, args.count, published_topics=topics)
        print(f"Generated {len(briefs)} briefs.\n")
        briefs = screen_briefs_against_history(briefs, topics, existing, args.count)

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
