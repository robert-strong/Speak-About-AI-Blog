#!/usr/bin/env python3
"""
from_sheet.py
-------------
Read the next "Queued" row from a Google Sheet, run create_blog_entry.py
with that row's values, then update the row's Status and Entry URL.

ONE-TIME SETUP
--------------
1. Enable the Google Sheets API in your Google Cloud project (the same
   project that owns your AI Studio API key, or any project):
   https://console.cloud.google.com/apis/library/sheets.googleapis.com
2. APIs & Services -> Credentials -> "Create credentials" -> Service Account
       Name: cowork-blog-automation
       Role: (none required for sheets)
3. On the new service account: Keys -> Add Key -> JSON -> Download.
   Save the downloaded JSON as `service_account.json` next to this script
   (or set GOOGLE_SERVICE_ACCOUNT_FILE env var to its path).
4. Open the Google Sheet you'll use as the queue. Click Share. Add the
   service account's email (looks like
   cowork-blog-automation@<project>.iam.gserviceaccount.com) with Editor
   access.
5. Copy the sheet ID from its URL:
       https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit#gid=0
   Set SHEET_ID env var or pass --sheet-id on each run.

INSTALL
-------
    pip install gspread google-auth python-dotenv requests

SHEET COLUMNS (header row, row 1)
---------------------------------
Required: Status, Title, Body Path, Excerpt, Meta Description,
          Image Prompt, Category
Optional: Tags, SEO Keywords, Slug, Display Title, Speakers, Author ID
Outputs:  Entry URL, Last Run, Notes  (script writes to these)

Status values:
    Queued       - script will pick up
    Processing   - in flight
    Created      - done; Entry URL filled in
    Error        - see Notes

USAGE
-----
    python3 from_sheet.py                # process next Queued row
    python3 from_sheet.py --all          # process every Queued row
    python3 from_sheet.py --row 5        # process row 5 specifically
    python3 from_sheet.py --dry-run      # show what would run, don't execute
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(".env.local")
except ImportError:
    pass

import gspread
from google.oauth2.service_account import Credentials


SHEET_ID = os.environ.get("SHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                                      "service_account.json")
WORKSHEET_NAME = os.environ.get("SHEET_WORKSHEET", "Sheet1")

REQUIRED_COLS = ["Status", "Title", "Body Path", "Excerpt",
                 "Meta Description", "Image Prompt", "Category"]


def open_sheet(sheet_id, worksheet_name):
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


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sheet-id", default=SHEET_ID)
    p.add_argument("--worksheet", default=WORKSHEET_NAME)
    p.add_argument("--all", action="store_true",
                   help="Process every Queued row in one run")
    p.add_argument("--row", type=int, default=None,
                   help="Process this specific 1-indexed row (ignores Status)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would run without executing")
    p.add_argument("--force", action="store_true",
                   help="Re-run rows that already have an Entry URL (creates duplicates)")
    args = p.parse_args()

    if not args.sheet_id:
        sys.exit("SHEET_ID env var or --sheet-id required.")
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        sys.exit(f"Service account file not found at {SERVICE_ACCOUNT_FILE}. "
                 "See setup instructions at the top of this script.")

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
    if args.all:
        for r in queued:
            process_row(ws, headers, r, dry_run=args.dry_run, force=args.force)
    else:
        process_row(ws, headers, queued[0], dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
