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
    --image-model       'imagen-3' (default, uses imagen-4.0-generate-001 with
                        aspect ratio support) or 'gemini-2.5-flash'
    --aspect-ratio      For imagen only. Default '16:9'. Other: '1:1','9:16','3:4','4:3'.
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
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
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

def generate_image_imagen3(prompt, aspect_ratio="16:9"):
    """Uses imagen-4.0-generate-001 (current Imagen model on AI Studio)."""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"imagen-4.0-generate-001:predict?key={GOOGLE_KEY}")
    payload = {"instances": [{"prompt": prompt}],
               "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio}}
    r = requests.post(url, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Imagen error {r.status_code}: {r.text}")
    preds = (r.json().get("predictions") or [])
    if not preds or "bytesBase64Encoded" not in preds[0]:
        raise RuntimeError(f"Unexpected Imagen response: {r.text}")
    return base64.b64decode(preds[0]["bytesBase64Encoded"])


def generate_image_gemini(prompt):
    """Uses gemini-2.5-flash-image (Nano Banana)."""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash-image:generateContent?key={GOOGLE_KEY}")
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"responseModalities": ["IMAGE"]}}
    r = requests.post(url, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Gemini image error {r.status_code}: {r.text}")
    for cand in r.json().get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise RuntimeError(f"No image bytes in Gemini response: {r.text}")


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


def generate_display_title(full_title):
    """Convert a full blog title into a 3-6 word display title for the stage backdrop.
    Tries Gemini Flash first; falls back to a heuristic on any error."""
    if not GOOGLE_KEY:
        return _heuristic_display_title(full_title)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={GOOGLE_KEY}")
    prompt = (
        "You write punchy display titles for corporate event stage backdrops. "
        "Convert the blog post title below into a 3-6 word display title. "
        "Drop filler words like 'how to', 'for your', 'the ultimate'. "
        "Use title case. Reply with ONLY the display title, no quotes, no extra text.\n\n"
        f"Title: {full_title}"
    )
    try:
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]},
                          timeout=20)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
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
    p.add_argument("--image-model", default="imagen-3",
                   choices=["imagen-3", "gemini-2.5-flash"])
    p.add_argument("--aspect-ratio", default="16:9")
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
        if args.image_model == "imagen-3":
            img_bytes = generate_image_imagen3(full_prompt, args.aspect_ratio)
        else:
            img_bytes = generate_image_gemini(full_prompt)
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
