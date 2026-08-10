#!/usr/bin/env python3
"""
enrich_post.py
--------------
Enrich a Contentful blog post with speaker photos and profile-page links.

Reads a speakers.json mapping (speaker name -> profile URL + headshot URL),
walks the entry's Rich Text body, and for each heading or paragraph whose
text content EXACTLY MATCHES a speaker name on its own line, inserts an
embedded photo asset above it and replaces the plain text with a hyperlink
to the speaker's profile page.

Updates the entry as a DRAFT revision. Does NOT republish — the live
published version is untouched until you click Publish in Contentful.

USAGE
    python3 enrich_post.py --entry-id <CONTENTFUL_ENTRY_ID>
    python3 enrich_post.py --entry-id <ID> --dry-run
    python3 enrich_post.py --entry-id <ID> --speakers other_speakers.json

Find the entry ID by opening the post in Contentful — the URL is
    https://app.contentful.com/spaces/<SPACE>/environments/master/entries/<ENTRY_ID>

Required env (loaded from .env / .env.local or set externally):
    CONTENTFUL_CMA_TOKEN
    CONTENTFUL_SPACE_ID
    CONTENTFUL_ENVIRONMENT  (default: master)
    CONTENTFUL_LOCALE        (default: en-US)

speakers.json format (an array of speaker objects):
    [
      {
        "name": "Adam Cheyer",
        "url": "https://speakabout.ai/speakers/adam-cheyer",
        "photo_url": "https://speakabout.ai/cdn/.../adam-cheyer.jpg"
      },
      ...
    ]

The first time the script encounters each speaker, it downloads the photo,
uploads it to Contentful as a published asset, and caches the asset ID in
.speaker_assets_cache.json (gitignored). Future runs reuse those asset IDs
and skip re-uploading.

REQUIRES the blogPost content type's Rich Text "content" field to permit
embedded-asset-block nodes. If the API rejects with a validation error,
go to Contentful -> Content model -> blogPost -> Content field -> Validations
-> Accept only specified entry/asset types -> ensure embedded asset blocks
are allowed.
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

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


CMA_TOKEN = os.environ.get("CONTENTFUL_CMA_TOKEN")
SPACE_ID = os.environ.get("CONTENTFUL_SPACE_ID")
ENV_ID = os.environ.get("CONTENTFUL_ENVIRONMENT", "master")
LOCALE = os.environ.get("CONTENTFUL_LOCALE", "en-US")

CMA_BASE = f"https://api.contentful.com/spaces/{SPACE_ID}/environments/{ENV_ID}"
UPLOAD_BASE = f"https://upload.contentful.com/spaces/{SPACE_ID}"

CMA_HEADERS = {
    "Authorization": f"Bearer {CMA_TOKEN}",
    "Content-Type": "application/vnd.contentful.management.v1+json",
}

CACHE_FILE = Path(".speaker_assets_cache.json")


# --- Speaker DB + cache ----------------------------------------------------

def load_speakers(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        sys.exit(f"{path} must be a JSON array of speaker objects.")
    return data


def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"   WARNING: {CACHE_FILE} is corrupt; starting fresh", file=sys.stderr)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def slugify(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


# --- Photo download + Contentful asset upload ------------------------------

MIME_EXTS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}

EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "avif": "image/avif",
}


def download_photo(url):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (enrich_post)"})
    r.raise_for_status()
    content_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
    return r.content, content_type


def get_photo_bytes(speaker):
    """Return (bytes, content_type) from either a local photo_path or remote photo_url."""
    photo_path = speaker.get("photo_path")
    if photo_path:
        path = Path(photo_path)
        if not path.exists():
            raise ValueError(f"Photo file not found: {photo_path}")
        ext = path.suffix.lower().lstrip(".")
        content_type = EXT_TO_MIME.get(ext, "image/jpeg")
        return path.read_bytes(), content_type
    photo_url = speaker.get("photo_url")
    if not photo_url:
        raise ValueError(f"Speaker '{speaker.get('name', '?')}' has no photo_path or photo_url")
    return download_photo(photo_url)


def upload_and_publish_asset(image_bytes, content_type, file_name, title, description):
    """Raw upload -> create asset -> process -> publish. Returns asset ID."""
    # 1. Raw upload
    up = requests.post(
        f"{UPLOAD_BASE}/uploads",
        headers={
            "Authorization": f"Bearer {CMA_TOKEN}",
            "Content-Type": "application/octet-stream",
        },
        data=image_bytes,
        timeout=60,
    )
    up.raise_for_status()
    upload_id = up.json()["sys"]["id"]

    # 2. Create asset entry
    asset_body = {
        "fields": {
            "title": {LOCALE: title},
            "description": {LOCALE: description},
            "file": {
                LOCALE: {
                    "contentType": content_type,
                    "fileName": file_name,
                    "uploadFrom": {"sys": {"type": "Link", "linkType": "Upload", "id": upload_id}},
                }
            },
        }
    }
    a = requests.post(f"{CMA_BASE}/assets", headers=CMA_HEADERS,
                      data=json.dumps(asset_body), timeout=60)
    a.raise_for_status()
    asset = a.json()
    asset_id = asset["sys"]["id"]
    version = asset["sys"]["version"]

    # 3. Process
    proc = requests.put(
        f"{CMA_BASE}/assets/{asset_id}/files/{LOCALE}/process",
        headers={**CMA_HEADERS, "X-Contentful-Version": str(version)},
        timeout=60,
    )
    proc.raise_for_status()

    # 4. Poll for processing completion
    final_version = version
    for _ in range(30):
        time.sleep(1)
        check = requests.get(f"{CMA_BASE}/assets/{asset_id}",
                             headers=CMA_HEADERS, timeout=30)
        check.raise_for_status()
        body = check.json()
        ff = body.get("fields", {}).get("file", {}).get(LOCALE, {})
        if ff.get("url"):
            final_version = body["sys"]["version"]
            break

    # 5. Publish (so the asset can be embedded in published entries later)
    pub = requests.put(
        f"{CMA_BASE}/assets/{asset_id}/published",
        headers={**CMA_HEADERS, "X-Contentful-Version": str(final_version)},
        timeout=30,
    )
    if not pub.ok:
        print(f"   WARNING: asset {asset_id} created but not published "
              f"({pub.status_code}: {pub.text[:200]})", file=sys.stderr)

    return asset_id


def ensure_speaker_asset(speaker, cache):
    """Get or upload the asset for a speaker. Returns asset ID.
    The asset's description encodes the speaker's profile URL using the marker
    'speaker-link:<URL>' so a frontend renderer can wrap the image in <a href>."""
    name = speaker["name"]
    if name in cache:
        return cache[name]

    print(f"  Loading photo for {name}...")
    img_bytes, content_type = get_photo_bytes(speaker)
    ext = MIME_EXTS.get(content_type, "jpg")
    print(f"  Uploading to Contentful (size: {len(img_bytes):,} bytes, type: {content_type})...")
    asset_id = upload_and_publish_asset(
        img_bytes,
        content_type,
        file_name=f"{slugify(name)}.{ext}",
        title=name,
        description=f"Headshot of {name}. speaker-link:{speaker['url']}",
    )
    cache[name] = asset_id
    save_cache(cache)
    print(f"  Asset ID: {asset_id}")
    return asset_id


# --- Rich Text walk + transform --------------------------------------------

def extract_text(node):
    """Recursively get plain text from a Rich Text node."""
    if node.get("nodeType") == "text":
        return node.get("value", "")
    return "".join(extract_text(c) for c in node.get("content", []))


def find_speaker(text, speakers_by_lower):
    """Match text to a speaker. Case-insensitive exact match on whole text."""
    t = text.strip().lower()
    return speakers_by_lower.get(t)


def make_link_node(node_type, name, url):
    """Build a heading/paragraph node containing the name as a hyperlink."""
    return {
        "nodeType": node_type,
        "data": {},
        "content": [{
            "nodeType": "hyperlink",
            "data": {"uri": url},
            "content": [{
                "nodeType": "text",
                "value": name,
                "marks": [],
                "data": {},
            }],
        }],
    }


def make_photo_node(asset_id):
    """Build an embedded-asset-block referencing the photo asset."""
    return {
        "nodeType": "embedded-asset-block",
        "data": {"target": {"sys": {"type": "Link", "linkType": "Asset", "id": asset_id}}},
        "content": [],
    }


def enrich_richtext(doc, speakers_by_lower, cache, dry_run=False):
    """Walk top-level content. For each heading/paragraph that exactly matches
    a speaker name, insert a photo asset before and replace the text with a
    hyperlinked version. Returns list of matched speaker names."""
    if doc.get("nodeType") != "document":
        raise ValueError(f"Expected document node, got {doc.get('nodeType')}")

    new_content = []
    matches = []
    for node in doc.get("content", []):
        node_type = node.get("nodeType", "")
        if node_type.startswith("heading-") or node_type == "paragraph":
            plain = extract_text(node).strip()
            speaker = find_speaker(plain, speakers_by_lower)
            if speaker:
                matches.append(speaker["name"])
                if dry_run:
                    new_content.append(node)
                    continue
                asset_id = ensure_speaker_asset(speaker, cache)
                new_content.append(make_photo_node(asset_id))
                new_content.append(make_link_node(node_type, speaker["name"], speaker["url"]))
                continue
        new_content.append(node)

    doc["content"] = new_content
    return matches


# --- Contentful entry I/O --------------------------------------------------

def get_entry(entry_id):
    r = requests.get(f"{CMA_BASE}/entries/{entry_id}", headers=CMA_HEADERS, timeout=30)
    if not r.ok:
        sys.exit(f"Could not fetch entry {entry_id}: {r.status_code} {r.text[:300]}")
    return r.json()


def update_entry(entry_id, fields, version):
    body = {"fields": fields}
    r = requests.put(
        f"{CMA_BASE}/entries/{entry_id}",
        headers={**CMA_HEADERS, "X-Contentful-Version": str(version)},
        data=json.dumps(body),
        timeout=60,
    )
    if not r.ok:
        sys.exit(f"Update failed: {r.status_code} {r.text[:500]}")
    return r.json()


# --- Main ------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Enrich a Contentful blog post with speaker photos and links.")
    p.add_argument("--entry-id", required=True, help="Contentful entry ID of the blog post")
    p.add_argument("--speakers", default="speakers.json", help="Path to speakers JSON file (default: speakers.json)")
    p.add_argument("--field", default="content", help="Rich Text field to enrich (default: content)")
    p.add_argument("--dry-run", action="store_true", help="Show matches without modifying anything")
    args = p.parse_args()

    if not CMA_TOKEN or not SPACE_ID:
        sys.exit("CONTENTFUL_CMA_TOKEN and CONTENTFUL_SPACE_ID are required.")
    if not Path(args.speakers).exists():
        sys.exit(f"Speakers file not found: {args.speakers}\n"
                 f"See enrich_post.py docstring for the expected format.")

    speakers = load_speakers(args.speakers)
    speakers_by_lower = {s["name"].strip().lower(): s for s in speakers if s.get("name")}
    print(f"Loaded {len(speakers)} speakers from {args.speakers}")

    cache = load_cache()
    print(f"Photo asset cache: {len(cache)} known")

    print(f"Fetching entry {args.entry_id}...")
    entry = get_entry(args.entry_id)
    title = entry.get("fields", {}).get("title", {}).get(LOCALE, "(no title)")
    version = entry["sys"]["version"]
    print(f"   Entry: '{title}' (version {version})")

    body = entry["fields"].get(args.field, {}).get(LOCALE)
    if not body:
        sys.exit(f"Entry has no '{args.field}' field for locale {LOCALE}.")

    enriched = copy.deepcopy(body)
    matches = enrich_richtext(enriched, speakers_by_lower, cache, dry_run=args.dry_run)

    if not matches:
        print("\nNo speaker mentions matched in the entry's body.")
        print("Tip: speaker names must appear EXACTLY (case-insensitive) as a")
        print("     heading or paragraph containing JUST the name and nothing else.")
        return

    print(f"\nMatched {len(matches)} speaker mention(s):")
    print(f"\nMatched {len(matches)} speaker mention(s):")
    for m in matches:
        print(f"   - {m}")

    if args.dry_run:
        print("\n[dry-run] Not modifying entry. Re-run without --dry-run to apply.")
        return

    print(f"\nUpdating entry {args.entry_id} as DRAFT...")
    new_fields = {**entry["fields"], args.field: {LOCALE: enriched}}
    updated = update_entry(args.entry_id, new_fields, version)
    print(f"   Saved as version {updated['sys']['version']}.")
    print(f"\nDone. Live published version is unchanged.")
    print(f"  https://app.contentful.com/spaces/{SPACE_ID}/environments/{ENV_ID}/entries/{args.entry_id}")
    print(f"Then click 'Publish changes' in Contentful when satisfied.")


if __name__ == "__main__":
    main()
