#!/usr/bin/env python3
"""
create_blog_entry.py
--------------------
Generate a hero image with Google Gemini/Imagen, upload it to Contentful as a
draft asset, and create a draft blog post entry in Contentful with all fields
populated. Nothing is published — everything lands as a draft for human review.

USAGE
-----
    python3 create_blog_entry.py \
        --title "How to Choose an AI Keynote Speaker" \
        --markdown article.md \
        --excerpt "A practical guide for event planners weighing AI experts." \
        --meta-description "Five questions every event planner should ask..." \
        --image-prompt "Minimalist editorial illustration of a microphone with..."

Optional flags:
    --image-file        Path to a local PNG/JPEG/WebP/GIF to use as hero (skips
                        image generation entirely). Use this when Google image
                        gen isn't available or you have your own asset.
                        --image-prompt is required UNLESS this is set.
    --slug              Override auto-generated slug (default: derived from title)
    --category          Category short text (default: blank)
    --tags              Comma-separated tags
    --seo-keywords      SEO keywords short text
    --speakers          Comma-separated speaker names
    --image-model       'gemini-3.1-flash' (default) - uses gemini-3.1-flash-image
    --aspect-ratio      Aspect ratio guidance. Default '16:9'. Other: '1:1','9:16','3:4','4:3'.
    --style-reference   URL to a reference image for style guidance
    --style-description Additional description for image style/look
    --locale            Default 'en-US'. Override if your space uses a different locale.

REQUIRED ENV VARS (or place in a .env file beside this script)
--------------------------------------------------------------
    CONTENTFUL_CMA_TOKEN     Personal access token (CFPAT-...)
    CONTENTFUL_SPACE_ID      e.g. 2ssjs5z6qgs3
    CONTENTFUL_ENVIRONMENT   e.g. master
    GOOGLE_AI_API_KEY        Google AI Studio key — only required when generating
                             images (i.e. when --image-file is NOT used)

OPTIONAL ENV VARS
-----------------
    CONTENTFUL_CONTENT_TYPE  default 'blogPost'
    CONTENTFUL_LOCALE        default 'en-US'

INSTALL
-------
    pip install requests python-dotenv

WHAT YOU GET IN CONTENTFUL
--------------------------
1. A new draft asset (the hero image)
2. A new draft entry of content type `blogPost` with:
       title, slug, metaDescription, excerpt, content (Rich Text),
       featuredImage (linked to the new asset),
       category, tags, seoKeywords, speakers (if provided)
Both are unpublished. Open them in Contentful, review, and click Publish.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(".env.local")
except ImportError:
    pass


CMA_TOKEN = os.environ.get("CONTENTFUL_CMA_TOKEN")
SPACE_ID = os.environ.get("CONTENTFUL_SPACE_ID")
ENV_ID = os.environ.get("CONTENTFUL_ENVIRONMENT", "master")
CONTENT_TYPE = os.environ.get("CONTENTFUL_CONTENT_TYPE", "blogPost")
LOCALE = os.environ.get("CONTENTFUL_LOCALE", "en-US")
GOOGLE_KEY = os.environ.get("GOOGLE_AI_API_KEY")

CMA_BASE = f"https://api.contentful.com/spaces/{SPACE_ID}/environments/{ENV_ID}"
UPLOAD_BASE = f"https://upload.contentful.com/spaces/{SPACE_ID}"

CMA_HEADERS = {
    "Authorization": f"Bearer {CMA_TOKEN}",
    "Content-Type": "application/vnd.contentful.management.v1+json",
}

MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

# Defaults for the blogPost content type
DEFAULT_AUTHOR_ID = "1VbdoaPazuvwGFuLwaZR6O"  # Robert Strong
DEFAULT_STATUS = "Waiting For Approval"
CATEGORY_OPTIONS = [
    "AI Speakers",
    "Event Planning",
    "Industry Insights",
    "Speaker Spotlight",
    "Company News",
]


_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?"
    r"(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _normalize_iso_date(s):
    """Normalize date strings to ISO 8601 for Contentful's Date field.
    Handles Google Sheets' rendering of ISO datetimes (e.g. '2026-05-04 9:00:00')
    that Contentful's API rejects."""
    s = (s or "").strip()
    if not s:
        return s
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    m = _DATE_RE.match(s)
    if not m:
        raise ValueError(f"Cannot parse published date: {s!r}")
    y, mo, d, h, mi, se, tz = m.groups()
    base = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(h):02d}:{int(mi):02d}:{int(se or 0):02d}"
    if tz:
        if tz != "Z" and ":" not in tz:
            tz = tz[:3] + ":" + tz[3:]
        base += tz
    return datetime.fromisoformat(base.replace("Z", "+00:00")).isoformat()


# --- Markdown -> Contentful Rich Text ---------------------------------------

INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
INLINE_ITALIC = re.compile(r"\*([^*]+)\*")


def _text_node(value, marks=None):
    return {
        "nodeType": "text",
        "value": value,
        "marks": [{"type": m} for m in (marks or [])],
        "data": {},
    }


def parse_inline(text):
    nodes = []
    i = 0
    while i < len(text):
        m = INLINE_LINK.match(text, i)
        if m:
            nodes.append({
                "nodeType": "hyperlink",
                "data": {"uri": m.group(2)},
                "content": [_text_node(m.group(1))],
            })
            i = m.end(); continue
        m = INLINE_BOLD.match(text, i)
        if m:
            nodes.append(_text_node(m.group(1), ["bold"]))
            i = m.end(); continue
        m = INLINE_ITALIC.match(text, i)
        if m:
            nodes.append(_text_node(m.group(1), ["italic"]))
            i = m.end(); continue
        next_special = re.search(r"[\[\*]", text[i + 1:])
        end = i + 1 + next_special.start() if next_special else len(text)
        nodes.append(_text_node(text[i:end]))
        i = end
    merged = []
    for n in nodes:
        if (merged and n["nodeType"] == "text" and not n["marks"]
                and merged[-1]["nodeType"] == "text" and not merged[-1]["marks"]):
            merged[-1]["value"] += n["value"]
        else:
            merged.append(n)
    return merged or [_text_node("")]


def markdown_to_richtext(md):
    blocks = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            blocks.append({"nodeType": f"heading-{level}", "data": {},
                           "content": parse_inline(m.group(2))})
            i += 1; continue
        if re.match(r"^[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                t = re.sub(r"^[-*]\s+", "", lines[i].rstrip())
                items.append({"nodeType": "list-item", "data": {},
                              "content": [{"nodeType": "paragraph", "data": {},
                                           "content": parse_inline(t)}]})
                i += 1
            blocks.append({"nodeType": "unordered-list", "data": {}, "content": items})
            continue
        if re.match(r"^\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                t = re.sub(r"^\d+\.\s+", "", lines[i].rstrip())
                items.append({"nodeType": "list-item", "data": {},
                              "content": [{"nodeType": "paragraph", "data": {},
                                           "content": parse_inline(t)}]})
                i += 1
            blocks.append({"nodeType": "ordered-list", "data": {}, "content": items})
            continue
        if line.startswith(">"):
            ql = []
            while i < len(lines) and lines[i].startswith(">"):
                ql.append(re.sub(r"^>\s?", "", lines[i].rstrip()))
                i += 1
            blocks.append({"nodeType": "blockquote", "data": {},
                           "content": [{"nodeType": "paragraph", "data": {},
                                        "content": parse_inline(" ".join(ql))}]})
            continue
        para = [line]; i += 1
        while (i < len(lines) and lines[i].strip()
               and not re.match(r"^(#{1,6}\s|[-*]\s|\d+\.\s|>)", lines[i])):
            para.append(lines[i].rstrip()); i += 1
        blocks.append({"nodeType": "paragraph", "data": {},
                       "content": parse_inline(" ".join(para))})
    return {"nodeType": "document", "data": {}, "content": blocks}


# --- Image generation -------------------------------------------------------

# Retry image-gen calls on 429 (rate limit / quota) with exponential backoff.
# Many Imagen 429s are per-minute throttling that clear quickly; sustained
# 429s likely mean daily quota exhausted and will keep failing.
_IMAGE_RETRY_DELAYS = [15, 30, 60, 120]  # seconds; 4 retries total


def _post_with_retry(url, payload, label):
    """POST with exponential backoff on 429. Returns the final response."""
    for attempt, delay in enumerate([0] + _IMAGE_RETRY_DELAYS):
        if delay:
            print(f"   {label}: 429 rate-limited; retry {attempt}/{len(_IMAGE_RETRY_DELAYS)} "
                  f"in {delay}s...", file=sys.stderr)
            time.sleep(delay)
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code != 429:
            return r
    return r  # final 429 response after exhausting retries


def generate_image_gemini_flash(prompt, aspect_ratio="16:9", style_reference_url=None, style_description=None):
    """Uses gemini-3.1-flash-image (replaces deprecated imagen-4.0-generate-001).

    Args:
        prompt: The image generation prompt
        aspect_ratio: Image aspect ratio (used in prompt guidance)
        style_reference_url: Optional URL to a reference image for style guidance
        style_description: Optional additional description for image style
    """
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-3.1-flash-image:generateContent?key={GOOGLE_KEY}")

    # Build the prompt with style guidance
    full_prompt = prompt
    if style_description:
        full_prompt = f"{prompt}\n\nStyle guidance: {style_description}"
    if aspect_ratio != "16:9":
        full_prompt = f"{full_prompt}\n\nAspect ratio: {aspect_ratio}"

    # Build content parts
    parts = []

    # If we have a style reference image URL, fetch and include it
    if style_reference_url:
        try:
            img_response = requests.get(style_reference_url, timeout=30)
            if img_response.ok:
                img_data = base64.b64encode(img_response.content).decode('utf-8')
                # Detect mime type
                content_type = img_response.headers.get('content-type', 'image/png')
                parts.append({
                    "inlineData": {
                        "mimeType": content_type,
                        "data": img_data
                    }
                })
                parts.append({"text": f"Use the above image as a style reference. Generate a new image with this style: {full_prompt}"})
            else:
                parts.append({"text": full_prompt})
        except Exception as e:
            print(f"Warning: Could not fetch style reference image: {e}")
            parts.append({"text": full_prompt})
    else:
        parts.append({"text": full_prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]}
    }

    r = _post_with_retry(url, payload, "Gemini Flash")
    if not r.ok:
        raise RuntimeError(f"Gemini Flash error {r.status_code}: {r.text}")

    for cand in r.json().get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise RuntimeError(f"No image bytes in Gemini Flash response: {r.text}")


# Legacy function for backwards compatibility - redirects to new implementation
def generate_image_gemini(prompt):
    """Legacy wrapper - uses gemini-3.1-flash-image."""
    return generate_image_gemini_flash(prompt)


# --- Contentful asset + entry -----------------------------------------------

def upload_image_to_contentful(image_bytes, file_name, title, description,
                               content_type="image/png"):
    up = requests.post(
        f"{UPLOAD_BASE}/uploads",
        headers={"Authorization": f"Bearer {CMA_TOKEN}",
                 "Content-Type": "application/octet-stream"},
        data=image_bytes, timeout=60)
    up.raise_for_status()
    upload_id = up.json()["sys"]["id"]

    asset_body = {"fields": {
        "title": {LOCALE: title},
        "description": {LOCALE: description},
        "file": {LOCALE: {
            "contentType": content_type,
            "fileName": file_name,
            "uploadFrom": {"sys": {"type": "Link", "linkType": "Upload",
                                   "id": upload_id}}
        }}
    }}
    a = requests.post(f"{CMA_BASE}/assets", headers=CMA_HEADERS,
                      data=json.dumps(asset_body), timeout=60)
    a.raise_for_status()
    asset = a.json()
    asset_id = asset["sys"]["id"]
    version = asset["sys"]["version"]

    proc = requests.put(
        f"{CMA_BASE}/assets/{asset_id}/files/{LOCALE}/process",
        headers={**CMA_HEADERS, "X-Contentful-Version": str(version)},
        timeout=60)
    proc.raise_for_status()

    for _ in range(30):
        time.sleep(1)
        check = requests.get(f"{CMA_BASE}/assets/{asset_id}",
                             headers=CMA_HEADERS, timeout=30)
        check.raise_for_status()
        ff = check.json().get("fields", {}).get("file", {}).get(LOCALE, {})
        if ff.get("url"):
            break
    else:
        print("WARNING: asset processing didn't complete within 30s.",
              file=sys.stderr)
    return asset_id


def create_blog_entry(fields):
    body = {"fields": fields}
    r = requests.post(
        f"{CMA_BASE}/entries",
        headers={**CMA_HEADERS, "X-Contentful-Content-Type": CONTENT_TYPE},
        data=json.dumps(body), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Entry create failed {r.status_code}: {r.text}")
    return r.json()["sys"]["id"]


# --- Helpers ----------------------------------------------------------------

def slugify(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


DEFAULT_STYLE_FILE = "image_style.txt"


def load_style_preamble(style_file_arg):
    """Read the brand style preamble from a file, if it exists.
    Resolution order: --style-file arg > image_style.txt next to script > none."""
    candidates = []
    if style_file_arg:
        candidates.append(Path(style_file_arg))
    candidates.append(Path(__file__).parent / DEFAULT_STYLE_FILE)
    candidates.append(Path.cwd() / DEFAULT_STYLE_FILE)
    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8").strip()
    return ""


def compose_image_prompt(user_prompt, style_preamble, display_title=None):
    """Combine the brand style preamble with the per-post image prompt.
    If display_title is provided, instruct the model to render it on the navy backdrop."""
    parts = []
    if style_preamble:
        parts.append(style_preamble)
    if display_title:
        parts.append(f'The navy backdrop must clearly display the text "{display_title.upper()}" '
                     f'in bold white sans-serif uppercase letters, well-kerned, prominently centered. '
                     f'Render only this text on the backdrop and no other text anywhere in the image.')
    parts.append(f"Subject for this image: {user_prompt}")
    return "\n\n".join(parts)


def _heuristic_display_title(title):
    """Strip common filler words and trim to ~5 words."""
    words = title.split()
    drop = {"how", "to", "for", "your", "the", "a", "an", "of", "in", "on",
            "with", "and", "or", "next", "ultimate", "complete", "guide"}
    kept = [w for w in words if w.lower().rstrip(":,.") not in drop]
    if not kept:
        kept = words
    return " ".join(kept[:5])


def _display_title_via_anthropic(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=20,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _display_title_via_gemini(prompt):
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 64,
                "temperature": 0.7,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
        timeout=20,
    )
    r.raise_for_status()
    candidates = r.json().get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def generate_display_title(full_title):
    """Convert a full blog title into a 3-6 word display title for the stage backdrop.
    Routes through TEXT_PROVIDER (anthropic|gemini); falls back to a heuristic on any error."""
    provider = os.environ.get("TEXT_PROVIDER", "anthropic").lower()
    prompt = (
        "You write punchy, attention-grabbing display titles that go on stage backdrops "
        "and brand banners — like a magazine cover line or a movie poster tagline, NOT a "
        "literal abbreviation of the article title. The goal is to make a viewer curious "
        "and pull them in.\n\n"
        "Constraints:\n"
        "- 2-5 words\n"
        "- Strong verbs and concrete nouns; avoid abstract words like 'guide', 'overview', "
        "'introduction', 'approach', 'strategies', 'insights'\n"
        "- Title case or all-caps acceptable; no quotes; no punctuation at the end\n"
        "- Should feel bold, declarative, and a little provocative — never bland\n"
        "- Reply with ONLY the display title, no labels or preamble\n\n"
        "Examples (article title -> display title):\n"
        "  'How to Choose an AI Keynote Speaker for Your Next Event' -> 'PICK YOUR AI VOICE'\n"
        "  '5 Ways AI Speakers Elevate Sales Kickoffs' -> 'SALES, REWIRED'\n"
        "  'AI in Healthcare: A Practical Guide for Hospital Leaders' -> 'SMARTER CARE NOW'\n"
        "  'AI Ethics for Business Leaders' -> 'THE RIGHT KIND OF AI'\n"
        "  'How AI Is Changing the Insurance Industry' -> 'INSURANCE GETS SMART'\n"
        "  'Why Every Sales Kickoff Needs an AI Speaker' -> 'KICKOFF GOES AI'\n\n"
        f"Article title: {full_title}\n"
        "Display title:"
    )
    caller = _display_title_via_gemini if provider == "gemini" else _display_title_via_anthropic
    try:
        text = caller(prompt)
        if text:
            text = text.strip('"\'.,;:').strip()
            if 1 <= len(text.split()) <= 8 and len(text) <= 50:
                return text
    except Exception as e:
        print(f"   (display title API call failed: {e}; using heuristic)", file=sys.stderr)
    return _heuristic_display_title(full_title)


def required_env_check(needs_google):
    required = {"CONTENTFUL_CMA_TOKEN": CMA_TOKEN,
                "CONTENTFUL_SPACE_ID": SPACE_ID}
    if needs_google:
        required["GOOGLE_AI_API_KEY"] = GOOGLE_KEY
    missing = [k for k, v in required.items() if not v]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}.")


# --- Main -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Create a draft blog post in Contentful.")
    p.add_argument("--title", required=True)
    p.add_argument("--markdown", required=True)
    p.add_argument("--excerpt", required=True)
    p.add_argument("--meta-description", required=True)
    p.add_argument("--image-prompt", default=None,
                   help="Required unless --image-file is given.")
    p.add_argument("--image-file", default=None,
                   help="Local PNG/JPEG/WebP/GIF to use as hero (skips Google).")
    p.add_argument("--slug", default=None)
    p.add_argument("--category", default=None,
                   help=f"One of: {', '.join(CATEGORY_OPTIONS)}")
    p.add_argument("--tags", default=None)
    p.add_argument("--seo-keywords", default=None)
    p.add_argument("--speakers", default=None)
    p.add_argument("--author-id", default=DEFAULT_AUTHOR_ID,
                   help=f"Contentful entry ID for the author. Default: Robert Strong ({DEFAULT_AUTHOR_ID}).")
    p.add_argument("--status", default=DEFAULT_STATUS,
                   help=f"Status field value. Default: '{DEFAULT_STATUS}'.")
    p.add_argument("--image-model", default="gemini-3.1-flash",
                   choices=["gemini-3.1-flash"],
                   help="Image generation model (default: gemini-3.1-flash)")
    p.add_argument("--aspect-ratio", default="16:9")
    p.add_argument("--style-reference", default=None,
                   help="URL to a reference image for style guidance")
    p.add_argument("--style-description", default=None,
                   help="Additional description for image style/look")
    p.add_argument("--style-file", default=None,
                   help="Path to a brand-style preamble file. "
                        "Default: image_style.txt next to script if present.")
    p.add_argument("--no-style-preamble", action="store_true",
                   help="Disable the brand style preamble for this run.")
    p.add_argument("--display-title", default=None,
                   help="Short title to render on the navy backdrop in the hero image. "
                        "If omitted, auto-generated from --title via Gemini.")
    p.add_argument("--no-display-title", action="store_true",
                   help="Skip rendering any text on the backdrop in the hero image.")
    p.add_argument("--published-date", default=None,
                   help="ISO 8601 datetime for the Contentful publishedDate field "
                        "(e.g. 2026-05-04T09:00:00-04:00). Sets when the post should "
                        "be published; the entry itself still lands as a Draft.")
    p.add_argument("--locale", default=None)
    args = p.parse_args()

    if args.locale:
        global LOCALE
        LOCALE = args.locale

    needs_google = args.image_file is None
    required_env_check(needs_google=needs_google)

    if not args.image_file and not args.image_prompt:
        sys.exit("Either --image-prompt or --image-file is required.")

    if args.category and args.category not in CATEGORY_OPTIONS:
        sys.exit(f"Invalid --category '{args.category}'. "
                 f"Must be one of: {', '.join(CATEGORY_OPTIONS)}")

    md_path = Path(args.markdown)
    if not md_path.exists():
        sys.exit(f"Markdown file not found: {md_path}")
    md = md_path.read_text(encoding="utf-8")

    slug = args.slug or slugify(args.title)
    print(f"-> Title:  {args.title}")
    print(f"-> Slug:   {slug}")

    if args.image_file:
        ip = Path(args.image_file)
        if not ip.exists():
            sys.exit(f"Image file not found: {ip}")
        img_bytes = ip.read_bytes()
        ext = ip.suffix.lower().lstrip(".")
        content_type = MIME_BY_EXT.get(ext, "image/png")
        file_ext = ext if ext in MIME_BY_EXT else "png"
        print(f"-> Using local image: {ip} ({len(img_bytes):,} bytes, {content_type})")
    else:
        style = "" if args.no_style_preamble else load_style_preamble(args.style_file)
        if args.no_display_title:
            display_title = None
        elif args.display_title:
            display_title = args.display_title
        else:
            print("-> Generating display title from full title...")
            display_title = generate_display_title(args.title)
            print(f"   Display title: {display_title}")
        full_prompt = compose_image_prompt(args.image_prompt, style, display_title)
        if style:
            print(f"-> Style preamble loaded ({len(style)} chars)")
        print(f"-> Generating image with {args.image_model}...")
        if args.style_reference:
            print(f"   Style reference: {args.style_reference}")
        if args.style_description:
            print(f"   Style description: {args.style_description[:100]}...")
        img_bytes = generate_image_gemini_flash(
            full_prompt,
            aspect_ratio=args.aspect_ratio,
            style_reference_url=args.style_reference,
            style_description=args.style_description
        )
        content_type = "image/png"
        file_ext = "png"
        print(f"   Got {len(img_bytes):,} bytes")
        local_img = md_path.with_suffix(f".hero.{file_ext}")
        local_img.write_bytes(img_bytes)
        print(f"   Saved local copy: {local_img}")

    print("-> Uploading asset to Contentful...")
    asset_id = upload_image_to_contentful(
        img_bytes,
        file_name=f"{slug}-hero.{file_ext}",
        title=f"{args.title} hero image",
        description=(args.image_prompt or args.title)[:200],
        content_type=content_type,
    )
    print(f"   Asset ID: {asset_id} (DRAFT - not published)")

    rich = markdown_to_richtext(md)
    fields = {
        "title": {LOCALE: args.title},
        "slug": {LOCALE: slug},
        "metaDescription": {LOCALE: args.meta_description},
        "excerpt": {LOCALE: args.excerpt},
        "content": {LOCALE: rich},
        "featuredImage": {LOCALE: {"sys": {"type": "Link",
                                           "linkType": "Asset",
                                           "id": asset_id}}},
        "author": {LOCALE: {"sys": {"type": "Link",
                                    "linkType": "Entry",
                                    "id": args.author_id}}},
        "status": {LOCALE: args.status},
    }
    if args.category:
        fields["category"] = {LOCALE: args.category}
    if args.published_date:
        fields["publishedDate"] = {LOCALE: args.published_date}
    if args.tags:
        fields["tags"] = {LOCALE: [t.strip() for t in args.tags.split(",") if t.strip()]}
    if args.seo_keywords:
        fields["seoKeywords"] = {LOCALE: args.seo_keywords}
    if args.speakers:
        fields["speakers"] = {LOCALE: [s.strip() for s in args.speakers.split(",") if s.strip()]}

    print("-> Creating blog post entry...")
    entry_id = create_blog_entry(fields)
    print(f"   Entry ID: {entry_id} (DRAFT - not published)")

    edit_url = (f"https://app.contentful.com/spaces/{SPACE_ID}/environments/"
                f"{ENV_ID}/entries/{entry_id}")
    print(f"\nDone. Review here:\n  {edit_url}\n")


if __name__ == "__main__":
    main()
