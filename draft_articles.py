#!/usr/bin/env python3
"""
draft_articles.py
-----------------
Brief-driven drafting. For each row in the queue sheet with Status=Queued
and Body Path empty, generate via Gemini whatever fields are missing:
    Title, Body, Excerpt, Meta Description, Image Prompt,
    Category, Tags, SEO Keywords

You provide Brief (and Status=Queued). The drafter fills in the rest.
Anything you DO fill in by hand wins — the drafter only generates fields
left empty in the sheet.

Body is saved to drafts/<slug>.md (slug derived from the generated/given Title).

Run BEFORE from_sheet.py. Or use run_pipeline.py to run both in sequence.

USAGE
    python3 draft_articles.py                # draft all eligible rows
    python3 draft_articles.py --row 5        # specific row (ignores Status)
    python3 draft_articles.py --dry-run      # show plan, don't write
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # Python < 3.9 fallback (no auto DST)

# Force UTF-8 stdout/stderr on Windows so smart quotes & em-dashes render
# instead of becoming the � replacement char.
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


SHEET_ID = os.environ.get("SHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                                      "service_account.json")
WORKSHEET_NAME = os.environ.get("SHEET_WORKSHEET", "Sheet1")
GOOGLE_KEY = os.environ.get("GOOGLE_AI_API_KEY")  # not used here; kept for parity
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

DRAFTS_DIR = Path("drafts")
# Hybrid model setup: cheap Haiku for short fields, Sonnet for the long-form body.
SHORT_MODEL = "claude-haiku-4-5-20251001"   # title, excerpt, meta, image prompt, category, tags, SEO
BODY_MODEL = "claude-sonnet-4-6"            # full article body only
TEXT_MODEL = SHORT_MODEL                    # default if a caller doesn't override
ANTHROPIC_VERSION = "2023-06-01"

# Text-generation provider toggle. Default is Anthropic; set
# TEXT_PROVIDER=gemini in .env.local to route all text calls to Google Gemini
# (e.g. while Anthropic is unavailable). When set to gemini, the Anthropic
# model the caller asks for is mapped to the Gemini model below.
TEXT_PROVIDER = os.environ.get("TEXT_PROVIDER", "anthropic").lower()
ANTHROPIC_TO_GEMINI = {
    SHORT_MODEL: "gemini-2.5-flash",
    BODY_MODEL: "gemini-2.5-pro",
}

CATEGORY_OPTIONS = ["AI Speakers", "Event Planning", "Industry Insights",
                    "Speaker Spotlight", "Company News"]

# Published Date auto-assignment
PUBLISH_TZ_NAME = os.environ.get("PUBLISH_TZ", "America/New_York")
PUBLISH_DOW = int(os.environ.get("PUBLISH_DOW", "0"))   # 0=Mon..6=Sun
PUBLISH_HOUR = int(os.environ.get("PUBLISH_HOUR", "9"))  # local hour


# --- Gemini text generation ------------------------------------------------

def claude_text(prompt, max_tokens=4096, temperature=0.7, model=None):
    """Generate text via the configured provider (TEXT_PROVIDER env var).
    Defaults to Anthropic Claude; set TEXT_PROVIDER=gemini in .env.local to
    route through Google Gemini using ANTHROPIC_TO_GEMINI for model mapping."""
    chosen = model or TEXT_MODEL
    if TEXT_PROVIDER == "gemini":
        return _gemini_text(prompt, max_tokens, temperature, chosen)
    return _anthropic_text(prompt, max_tokens, temperature, chosen)


def _anthropic_text(prompt, max_tokens, temperature, model):
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"Claude error {r.status_code}: {r.text}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _gemini_text(prompt, max_tokens, temperature, model):
    if not GOOGLE_KEY:
        raise RuntimeError("GOOGLE_AI_API_KEY is not set.")
    gemini_model = ANTHROPIC_TO_GEMINI.get(model, "gemini-2.5-flash")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent",
        params={"key": GOOGLE_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        },
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text}")
    candidates = r.json().get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {r.text[:500]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty text: {r.text[:500]}")
    return text


def _strip_quotes(s):
    return s.strip().strip('"\'').strip()


# --- Prompts ---------------------------------------------------------------

TITLE_PROMPT = """You write blog post titles for Speak About AI, a premier AI keynote speakers bureau.
Audience: event planners, corporate marketers, sales/revenue leaders booking events.

Based on the brief below, write a compelling 8-14 word blog post title.
- Be specific and value-oriented, not generic
- No clickbait, no rhetorical questions, no "Ultimate Guide to..." filler
- Title case
- No quotes around it

Reply with ONLY the title.

Brief:
{brief}
"""

BODY_PROMPT = """You are a senior content writer for Speak About AI, a premier AI keynote speakers bureau.
Write a complete blog post in markdown format based on the title and brief below.

Title: {title}

Brief:
{brief}

Requirements:
- Length: ~1200-1800 words
- Use markdown ## for major sections (and ### for sub-sections if needed)
- Do NOT include the title as an H1 — start with an opening paragraph that hooks the reader
- Conversational but professional tone, like a knowledgeable colleague writing for event planners and business leaders
- Include concrete examples and specific actionable advice; avoid generic AI-thought-leadership filler
- No "In this article we will..." or "In conclusion..." style filler — just substantive content from the first sentence
- Use bold (**text**) sparingly to emphasize the most important takeaways
- Output the markdown body only — no preamble, no closing remarks, no meta-commentary
"""

EXCERPT_PROMPT = """Write a single-sentence excerpt (15-25 words) summarizing the article below for a blog post listing page.
Make it engaging and concrete, not generic.
Reply with ONLY the sentence — no quotes, no preamble, no labels.

Article:
{body}
"""

META_PROMPT = """Write an SEO meta description for the article below.
Constraints:
- 145-160 characters total
- Action-oriented, names the topic clearly
- Reads naturally, not stuffed with keywords

Reply with ONLY the meta description text — no quotes, no preamble.

Article:
{body}
"""

IMAGE_PROMPT_PROMPT = """Suggest a visual subject for the hero image of this blog post.
The image will be a documentary-style photograph of a corporate event scene with a navy stage backdrop. The backdrop and brand styling are handled separately.

Describe ONLY the human subject and composition in 1-2 short sentences.
Examples:
  "A keynote speaker mid-presentation gesturing toward the audience, with attendees visible in the foreground."
  "A panel of business leaders seated on a conference stage, mid-discussion, microphones in hand."

Do NOT mention the navy backdrop, color palette, lighting, or any text — those are handled by the style guide.
Reply with ONLY the description, no preamble.

Title: {title}
Brief:
{brief}
"""

CATEGORY_PROMPT = """Pick the best category for this blog post.
Options (use one of these EXACTLY):
  AI Speakers
  Event Planning
  Industry Insights
  Speaker Spotlight
  Company News

Definitions:
  AI Speakers      = profiles, lists, or comparisons of AI keynote speakers
  Event Planning   = practical guides for planners booking/hosting events
  Industry Insights = analysis of trends, tech, business in AI/events space
  Speaker Spotlight = deep-dive on one specific speaker
  Company News     = announcements about Speak About AI itself

Reply with ONLY the category name, exact spelling.

Title: {title}
Brief:
{brief}
"""

TAGS_PROMPT = """Suggest 4-6 short tags for this blog post for organizational tagging.
- Single words or 2-word phrases
- Comma-separated, no quotes
- Lowercase except proper nouns/acronyms (e.g., 'AI', 'B2B')

Example: keynote, AI, event planning, speaker selection, B2B events

Reply with ONLY the comma-separated tags, no preamble.

Title: {title}
Brief:
{brief}
"""

SEO_PROMPT = """Suggest 5-8 long-tail SEO keyword phrases someone might type into Google to find this article.
- Comma-separated, no quotes
- Total length under 200 characters
- Mix of short head terms and long-tail phrases
- Lowercase except acronyms

Example: AI keynote speaker, how to choose AI keynote speaker, AI speaker bureau, hire AI keynote, event planning AI

Reply with ONLY the comma-separated keywords, no preamble.

Title: {title}
Brief:
{brief}
"""


# --- Generators (one per field) -------------------------------------------

def draft_title(brief):
    out = claude_text(TITLE_PROMPT.format(brief=brief),
                      max_tokens=256, temperature=0.7)
    return _strip_quotes(out)


def draft_body(title, brief):
    out = claude_text(BODY_PROMPT.format(title=title, brief=brief),
                      max_tokens=4096, temperature=0.7, model=BODY_MODEL)
    out = re.sub(r"^#\s+.+\n+", "", out, count=1)
    return out.strip()


def draft_excerpt(body):
    out = claude_text(EXCERPT_PROMPT.format(body=body[:6000]),
                      max_tokens=512, temperature=0.5)
    return _strip_quotes(out)


def draft_meta(body):
    out = claude_text(META_PROMPT.format(body=body[:6000]),
                      max_tokens=512, temperature=0.5)
    out = _strip_quotes(out)
    if len(out) > 160:
        out = out[:157].rstrip(",.;: ") + "..."
    return out


def draft_image_prompt(title, brief):
    out = claude_text(IMAGE_PROMPT_PROMPT.format(title=title, brief=brief),
                      max_tokens=512, temperature=0.6)
    return _strip_quotes(out)


def draft_category(title, brief):
    out = claude_text(CATEGORY_PROMPT.format(title=title, brief=brief),
                      max_tokens=128, temperature=0.2)
    out = _strip_quotes(out)
    # Validate against allowed set; fall back if Gemini returns something off
    for opt in CATEGORY_OPTIONS:
        if out.lower() == opt.lower():
            return opt
    # Loose match
    for opt in CATEGORY_OPTIONS:
        if opt.lower() in out.lower():
            return opt
    return "Industry Insights"


def draft_tags(title, brief):
    out = claude_text(TAGS_PROMPT.format(title=title, brief=brief),
                      max_tokens=256, temperature=0.5)
    return _strip_quotes(out)


def draft_seo(title, brief):
    out = claude_text(SEO_PROMPT.format(title=title, brief=brief),
                      max_tokens=512, temperature=0.5)
    out = _strip_quotes(out)
    if len(out) > 256:
        out = out[:253].rstrip(", ") + "..."
    return out


# --- Sheet helpers ---------------------------------------------------------

def slugify(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def _publish_tz():
    if ZoneInfo:
        try:
            return ZoneInfo(PUBLISH_TZ_NAME)
        except Exception:
            return None
    return None


def _parse_iso(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _next_publish_slot(tz):
    """Next future Monday at PUBLISH_HOUR:00 in the configured timezone."""
    now = datetime.now(tz) if tz else datetime.now()
    days_ahead = (PUBLISH_DOW - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= PUBLISH_HOUR:
        days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=PUBLISH_HOUR, minute=0, second=0, microsecond=0)
    return target


def assign_publish_dates(ws, headers, rows):
    """For Queued rows missing a Published Date, assign weekly publish slots
    starting from after the latest existing date in the sheet."""
    if "Published Date" not in headers:
        print("  (Published Date column not present in sheet — skipping auto-assignment)")
        return
    tz = _publish_tz()
    existing = []
    for r in rows:
        dt = _parse_iso(r.get("Published Date", ""))
        if dt is None:
            continue
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
        existing.append(dt)
    next_slot = (max(existing) + timedelta(days=7)) if existing else _next_publish_slot(tz)
    queued_missing = sorted(
        [r for r in rows
         if r.get("Status", "").strip().lower() == "queued"
         and not r.get("Published Date", "").strip()],
        key=lambda x: x["__row__"]
    )
    if not queued_missing:
        return
    print(f"-> Auto-assigning Published Date to {len(queued_missing)} row(s) "
          f"(weekly, starting {next_slot.isoformat()})")
    for r in queued_missing:
        iso = next_slot.isoformat()
        r["Published Date"] = iso
        update(ws, r["__row__"], headers, "Published Date", iso)
        print(f"   Row {r['__row__']}: {iso}")
        next_slot += timedelta(days=7)


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


def read_rows(ws):
    all_values = ws.get_all_values()
    if not all_values:
        return [], []
    headers = all_values[0]
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        d = dict(zip(headers, row))
        d["__row__"] = i
        rows.append(d)
    return headers, rows


def col_index(headers, name):
    try:
        return headers.index(name) + 1
    except ValueError:
        return None


def update(ws, row, headers, col_name, value):
    idx = col_index(headers, col_name)
    if idx is None:
        print(f"   WARNING: column '{col_name}' missing — skipping update",
              file=sys.stderr)
        return
    ws.update_cell(row, idx, value)


def is_eligible(row_data):
    """A row is eligible for drafting if it's Queued with a Brief, hasn't been
    published yet (no Entry URL), and is missing any field needed for publishing.
    This includes rows from failed prior runs that have a Body Path but blank
    Excerpt / Meta Description / Image Prompt / Category."""
    status = row_data.get("Status", "").strip().lower()
    brief = row_data.get("Brief", "").strip()
    if status != "queued" or not brief:
        return False
    if row_data.get("Entry URL", "").strip():
        return False  # already published
    body_path = row_data.get("Body Path", "").strip()
    if not body_path or not Path(body_path).exists():
        return True
    for col in ("Title", "Excerpt", "Meta Description", "Image Prompt", "Category"):
        if not row_data.get(col, "").strip():
            return True
    return False


# --- Main ------------------------------------------------------------------

def process_row(ws, headers, row_data, dry_run=False):
    row_num = row_data["__row__"]
    brief = row_data.get("Brief", "").strip()
    if not brief:
        print(f"Row {row_num}: missing Brief, skipping")
        return False

    title_existing = row_data.get("Title", "").strip()
    print(f"\n=== Row {row_num} ===")
    print(f"  Brief: {brief[:140]}{'...' if len(brief) > 140 else ''}")

    if dry_run:
        plan = []
        if not title_existing: plan.append("Title")
        plan.append("Body")
        if not row_data.get("Excerpt", "").strip(): plan.append("Excerpt")
        if not row_data.get("Meta Description", "").strip(): plan.append("Meta Description")
        if not row_data.get("Image Prompt", "").strip(): plan.append("Image Prompt")
        if not row_data.get("Category", "").strip(): plan.append("Category")
        if not row_data.get("Tags", "").strip(): plan.append("Tags")
        if not row_data.get("SEO Keywords", "").strip(): plan.append("SEO Keywords")
        print(f"  Would generate: {', '.join(plan)}")
        return True

    if not GOOGLE_KEY:
        print("  ERROR: GOOGLE_AI_API_KEY not set", file=sys.stderr)
        update(ws, row_num, headers, "Notes", "GOOGLE_AI_API_KEY missing")
        return False

    DRAFTS_DIR.mkdir(exist_ok=True)

    try:
        update(ws, row_num, headers, "Last Run",
               datetime.utcnow().isoformat(timespec="seconds") + "Z")
        update(ws, row_num, headers, "Notes", "Drafting...")

        # 1. Title
        if title_existing:
            title = title_existing
            print(f"  Title (existing): {title}")
        else:
            print("  -> Generating title...")
            title = draft_title(brief)
            print(f"     {title}")
            update(ws, row_num, headers, "Title", title)

        slug = (row_data.get("Slug", "").strip() or slugify(title))

        # 2. Body — only generate if Body Path is empty or the file is missing.
        existing_body = row_data.get("Body Path", "").strip()
        if existing_body and Path(existing_body).exists():
            out_path = Path(existing_body)
            body = out_path.read_text(encoding="utf-8")
            print(f"  Body (existing, {len(body):,} chars): {out_path}")
        else:
            out_path = DRAFTS_DIR / f"{slug}.md"
            print("  -> Generating body...")
            body = draft_body(title, brief)
            out_path.write_text(body, encoding="utf-8")
            print(f"     Saved {len(body):,} chars to {out_path}")
            update(ws, row_num, headers, "Body Path", str(out_path))

        # 3. Excerpt (if missing)
        if not row_data.get("Excerpt", "").strip():
            print("  -> Generating excerpt...")
            excerpt = draft_excerpt(body)
            print(f"     {excerpt}")
            update(ws, row_num, headers, "Excerpt", excerpt)

        # 4. Meta description (if missing)
        if not row_data.get("Meta Description", "").strip():
            print("  -> Generating meta description...")
            meta = draft_meta(body)
            print(f"     ({len(meta)} chars) {meta}")
            update(ws, row_num, headers, "Meta Description", meta)

        # 5. Image prompt (if missing)
        if not row_data.get("Image Prompt", "").strip():
            print("  -> Generating image prompt...")
            img = draft_image_prompt(title, brief)
            print(f"     {img}")
            update(ws, row_num, headers, "Image Prompt", img)

        # 6. Category (if missing)
        if not row_data.get("Category", "").strip():
            print("  -> Picking category...")
            cat = draft_category(title, brief)
            print(f"     {cat}")
            update(ws, row_num, headers, "Category", cat)

        # 7. Tags (if missing)
        if not row_data.get("Tags", "").strip():
            print("  -> Generating tags...")
            tags = draft_tags(title, brief)
            print(f"     {tags}")
            update(ws, row_num, headers, "Tags", tags)

        # 8. SEO keywords (if missing)
        if not row_data.get("SEO Keywords", "").strip():
            print("  -> Generating SEO keywords...")
            seo = draft_seo(title, brief)
            print(f"     {seo}")
            update(ws, row_num, headers, "SEO Keywords", seo)

        update(ws, row_num, headers, "Notes", "Draft ready")
        return True
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        update(ws, row_num, headers, "Notes", f"Drafting failed: {e}"[:500])
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--row", type=int, default=None,
                   help="Process this specific row (ignores Status check)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not SHEET_ID:
        sys.exit("SHEET_ID env var required.")
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        sys.exit(f"Service account file not found at {SERVICE_ACCOUNT_FILE}.")

    ws = open_sheet()
    headers, rows = read_rows(ws)
    print(f"Sheet '{ws.title}' has {len(rows)} data rows")

    if "Brief" not in headers:
        sys.exit("Sheet is missing a 'Brief' column. Add it to the header row.")

    # Auto-assign Published Date to any Queued rows missing one (weekly Mondays).
    assign_publish_dates(ws, headers, rows)

    if args.row is not None:
        target = next((r for r in rows if r["__row__"] == args.row), None)
        if not target:
            sys.exit(f"Row {args.row} not found")
        process_row(ws, headers, target, dry_run=args.dry_run)
        return

    eligible = [r for r in rows if is_eligible(r)]
    if not eligible:
        print("No rows need drafting.")
        return
    print(f"Found {len(eligible)} row(s) needing drafts")
    for r in eligible:
        process_row(ws, headers, r, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
