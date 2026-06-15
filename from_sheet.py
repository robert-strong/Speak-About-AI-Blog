#!/usr/bin/env python3
"""
from_sheet.py
-------------
Read drafted items from the queue and publish them to Contentful via
create_blog_entry.py. Supports two backends:

1. REST API (preferred): Database-backed queue via speakabout.ai API
2. Google Sheets (legacy fallback): Read/write from a Google Sheet

USAGE
-----
    python3 from_sheet.py                # process next drafted item
    python3 from_sheet.py --limit 5      # process up to 5 items
    python3 from_sheet.py --all          # process every drafted item
    python3 from_sheet.py --row 5        # process row 5 (sheets only)
    python3 from_sheet.py --dry-run      # show what would run, don't execute
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Sleep between rows when processing --all, to stay under the per-minute image
# generation cap (Paid Tier 1: 10 imagen-4.0-generate requests/min). 10s spacing
# = max 6/min, well under the cap with margin for retries. Override via env.
INTER_ROW_DELAY = int(os.environ.get("INTER_ROW_DELAY", "10"))

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)
except ImportError:
    pass

# Only import Google Sheets libraries if needed
gspread = None
Credentials = None

USE_BLOG_API = os.environ.get("USE_BLOG_API", "").lower() == "true"

SHEET_ID = os.environ.get("SHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                                      "service_account.json")
WORKSHEET_NAME = os.environ.get("SHEET_WORKSHEET", "Sheet1")

# API client (lazy import)
_api_client = None


def _get_api_client():
    """Lazy-load the API client."""
    global _api_client
    if _api_client is None:
        from api_client import BlogPipelineAPI
        _api_client = BlogPipelineAPI()
    return _api_client


def _load_gspread():
    """Lazy-load Google Sheets libraries."""
    global gspread, Credentials
    if gspread is None:
        import gspread as gs
        from google.oauth2.service_account import Credentials as Creds
        gspread = gs
        Credentials = Creds

REQUIRED_COLS = ["Status", "Title", "Body Path", "Excerpt",
                 "Meta Description", "Image Prompt", "Category"]


def open_sheet(sheet_id, worksheet_name):
    _load_gspread()
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    book = gc.open_by_key(sheet_id)
    try:
        return book.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        # Fall back to the first sheet
        return book.get_worksheet(0)


def read_rows(ws):
    """Return (headers, list_of_dicts) where each dict has '__row__' set."""
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
    if idx:
        ws.update_cell(row, idx, value)


def process_row(ws, headers, row_data, dry_run=False, force=False):
    row_num = row_data["__row__"]
    title = row_data.get("Title", "").strip()
    print(f"\n=== Row {row_num}: {title or '(no title)'} ===")

    # Idempotency guard: if this row already has an Entry URL, don't republish.
    existing_url = row_data.get("Entry URL", "").strip()
    if existing_url and not force:
        print(f"  SKIP: row already has Entry URL ({existing_url}). "
              f"Use --force to republish.")
        return False

    # Empty-row guard: if every required field is blank, this is a placeholder row.
    # Don't mark it Error, just skip silently.
    if all(not row_data.get(c, "").strip() for c in REQUIRED_COLS):
        print("  SKIP: row is empty (all required fields blank).")
        return False

    missing = [c for c in REQUIRED_COLS
               if not row_data.get(c, "").strip()]
    if missing:
        err = f"Missing required field(s): {', '.join(missing)}"
        print(f"  ERROR: {err}")
        if not dry_run:
            update(ws, row_num, headers, "Status", "Error")
            update(ws, row_num, headers, "Notes", err)
        return False

    body_path = Path(row_data["Body Path"].strip())
    if not body_path.exists():
        err = f"Body file not found: {body_path}"
        print(f"  ERROR: {err}")
        if not dry_run:
            update(ws, row_num, headers, "Status", "Error")
            update(ws, row_num, headers, "Notes", err)
        return False

    cmd = [
        sys.executable, "create_blog_entry.py",
        "--title", title,
        "--markdown", str(body_path),
        "--excerpt", row_data["Excerpt"].strip(),
        "--meta-description", row_data["Meta Description"].strip(),
        "--image-prompt", row_data["Image Prompt"].strip(),
        "--category", row_data["Category"].strip(),
    ]
    for sheet_col, cli_flag in [
        ("Tags", "--tags"),
        ("SEO Keywords", "--seo-keywords"),
        ("Slug", "--slug"),
        ("Display Title", "--display-title"),
        ("Speakers", "--speakers"),
        ("Author ID", "--author-id"),
        ("Published Date", "--published-date"),
    ]:
        val = row_data.get(sheet_col, "").strip()
        if val:
            cmd.extend([cli_flag, val])

    print("  Command:")
    print("    " + " ".join(repr(a) for a in cmd[:6]) + " ...")
    if dry_run:
        print("  (dry-run; not executing)")
        return True

    update(ws, row_num, headers, "Status", "Processing")
    update(ws, row_num, headers, "Last Run",
           datetime.utcnow().isoformat(timespec="seconds") + "Z")
    update(ws, row_num, headers, "Notes", "")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        print(out)
        if proc.returncode != 0:
            update(ws, row_num, headers, "Status", "Error")
            update(ws, row_num, headers, "Notes", out[-500:].strip())
            return False
        m = re.search(r"https://app\.contentful\.com/spaces/[^\s]+/entries/[A-Za-z0-9]+",
                      out)
        if m:
            update(ws, row_num, headers, "Entry URL", m.group(0))
        update(ws, row_num, headers, "Status", "Created")
        return True
    except subprocess.TimeoutExpired:
        update(ws, row_num, headers, "Status", "Error")
        update(ws, row_num, headers, "Notes", "Timed out after 5 minutes")
        return False
    except Exception as e:
        update(ws, row_num, headers, "Status", "Error")
        update(ws, row_num, headers, "Notes", str(e)[:500])
        return False


def process_item_api(api, item, dry_run=False, force=False):
    """Process a single queue item via API and publish to Contentful."""
    item_id = item["id"]
    title = (item.get("title") or "").strip()
    print(f"\n=== Item {item_id}: {title or '(no title)'} ===")

    # Idempotency guard
    existing_url = (item.get("contentful_entry_url") or "").strip()
    if existing_url and not force:
        print(f"  SKIP: item already has Entry URL ({existing_url}). "
              f"Use --force to republish.")
        return False

    # Check required fields
    required = {
        "title": item.get("title"),
        "body_content": item.get("body_content"),
        "excerpt": item.get("excerpt"),
        "meta_description": item.get("meta_description"),
        "image_prompt": item.get("image_prompt"),
        "category": item.get("category"),
    }
    missing = [k for k, v in required.items() if not (v or "").strip()]
    if missing:
        err = f"Missing required field(s): {', '.join(missing)}"
        print(f"  ERROR: {err}")
        if not dry_run:
            api.update_item(item_id, status="error", error_message=err, notes=err)
        return False

    # Write body content to temp file for create_blog_entry.py
    import tempfile
    body_content = item["body_content"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body_content)
        body_path = f.name

    try:
        cmd = [
            sys.executable, "create_blog_entry.py",
            "--title", title,
            "--markdown", body_path,
            "--excerpt", item["excerpt"].strip(),
            "--meta-description", item["meta_description"].strip(),
            "--image-prompt", item["image_prompt"].strip(),
            "--category", item["category"].strip(),
        ]
        # Optional fields
        if item.get("tags"):
            tags = item["tags"]
            if isinstance(tags, list):
                tags = ", ".join(tags)
            cmd.extend(["--tags", tags])
        if item.get("seo_keywords"):
            cmd.extend(["--seo-keywords", item["seo_keywords"]])
        if item.get("slug"):
            cmd.extend(["--slug", item["slug"]])
        if item.get("display_title"):
            cmd.extend(["--display-title", item["display_title"]])
        if item.get("speakers"):
            speakers = item["speakers"]
            if isinstance(speakers, list):
                speakers = ", ".join(speakers)
            cmd.extend(["--speakers", speakers])
        if item.get("author_id"):
            cmd.extend(["--author-id", item["author_id"]])
        if item.get("published_date"):
            cmd.extend(["--published-date", item["published_date"]])

        # Image style settings (fetched from API settings)
        try:
            style_ref = api.get_setting("image_style_reference")
            if style_ref:
                cmd.extend(["--style-reference", style_ref])
        except:
            pass
        try:
            style_desc = api.get_setting("image_style_description")
            if style_desc:
                cmd.extend(["--style-description", style_desc])
        except:
            pass

        print("  Command:")
        print("    " + " ".join(repr(a) for a in cmd[:6]) + " ...")
        if dry_run:
            print("  (dry-run; not executing)")
            return True

        api.update_item(item_id, status="processing",
                       last_run=datetime.utcnow().isoformat(timespec="seconds") + "Z",
                       notes="Publishing to Contentful...")

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        print(out)
        if proc.returncode != 0:
            api.update_item(item_id, status="error",
                           error_message=out[-500:].strip(),
                           notes=out[-500:].strip())
            return False

        m = re.search(r"https://app\.contentful\.com/spaces/[^\s]+/entries/([A-Za-z0-9]+)", out)
        if m:
            entry_url = m.group(0)
            entry_id = m.group(1)
            api.update_item(item_id, status="created",
                           contentful_entry_url=entry_url,
                           contentful_entry_id=entry_id,
                           notes="Published to Contentful")
        else:
            api.update_item(item_id, status="created", notes="Published (no URL found in output)")
        return True
    except subprocess.TimeoutExpired:
        api.update_item(item_id, status="error",
                       error_message="Timed out after 5 minutes",
                       notes="Timed out after 5 minutes")
        return False
    except Exception as e:
        api.update_item(item_id, status="error",
                       error_message=str(e)[:500],
                       notes=str(e)[:500])
        return False
    finally:
        # Clean up temp file
        try:
            os.unlink(body_path)
        except:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sheet-id", default=SHEET_ID)
    p.add_argument("--worksheet", default=WORKSHEET_NAME)
    p.add_argument("--all", action="store_true",
                   help="Process every drafted item in one run")
    p.add_argument("--limit", type=int, default=None,
                   help="Maximum number of items to process")
    p.add_argument("--row", type=int, default=None,
                   help="Process this specific row (Google Sheets only)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would run without executing")
    p.add_argument("--force", action="store_true",
                   help="Re-run items that already have an Entry URL (creates duplicates)")
    args = p.parse_args()

    if USE_BLOG_API:
        # REST API backend
        print("Using REST API backend...")
        api = _get_api_client()

        # Get drafted items (ready for publishing)
        print("Fetching drafted items from API...")
        # The API returns queued items, but we need drafted ones
        # We'll fetch all and filter
        all_items = api.get_queued_items()  # This gets status=queued
        # Actually we need to get drafted items - let's use a different approach
        # For now, look for items with status 'drafted'
        # The api_client.get_queued_items returns status=queued, we need drafted
        # Let me check if API supports status filter... it doesn't directly
        # We'll need to filter client-side or update the API
        # For now, let's make a direct request for drafted items

        import requests
        base_url = api.base_url
        headers = api._headers()
        r = requests.get(f"{base_url}/queue", headers=headers, params={"status": "drafted"})
        if r.ok:
            drafted = r.json().get("items", [])
        else:
            print(f"Error fetching drafted items: {r.status_code}")
            drafted = []

        if not drafted:
            print("No drafted items. Nothing to do.")
            return

        print(f"Found {len(drafted)} drafted item(s)")

        if args.limit:
            drafted = drafted[:args.limit]
        elif not args.all:
            drafted = drafted[:1]

        print(f"Processing {len(drafted)} item(s)")
        success = 0
        for i, item in enumerate(drafted):
            if i > 0 and INTER_ROW_DELAY > 0 and not args.dry_run:
                print(f"\n(throttling: sleeping {INTER_ROW_DELAY}s)")
                time.sleep(INTER_ROW_DELAY)
            if process_item_api(api, item, dry_run=args.dry_run, force=args.force):
                success += 1

        print(f"\nPublished {success}/{len(drafted)} items")

    else:
        # Legacy Google Sheets backend
        print("Using Google Sheets backend (legacy)...")
        if not args.sheet_id:
            sys.exit("SHEET_ID env var or --sheet-id required (or set USE_BLOG_API=true).")
        if not Path(SERVICE_ACCOUNT_FILE).exists():
            sys.exit(f"Service account file not found at {SERVICE_ACCOUNT_FILE}.")

        ws = open_sheet(args.sheet_id, args.worksheet)
        headers, rows = read_rows(ws)
        print(f"Sheet '{ws.title}' has {len(rows)} data rows")

        if args.row is not None:
            target = next((r for r in rows if r["__row__"] == args.row), None)
            if not target:
                sys.exit(f"Row {args.row} not found")
            process_row(ws, headers, target, dry_run=args.dry_run, force=args.force)
            return

        queued = [r for r in rows
                  if r.get("Status", "").strip().lower() == "queued"]
        if not queued:
            print("No Queued rows. Nothing to do.")
            return
        print(f"Found {len(queued)} Queued row(s)")

        if args.limit:
            queued = queued[:args.limit]
        elif args.all:
            pass  # process all
        else:
            queued = queued[:1]

        for i, r in enumerate(queued):
            if i > 0 and INTER_ROW_DELAY > 0 and not args.dry_run:
                print(f"\n(throttling: sleeping {INTER_ROW_DELAY}s)")
                time.sleep(INTER_ROW_DELAY)
            process_row(ws, headers, r, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
