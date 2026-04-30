#!/usr/bin/env python3
"""
add_status_value.py
-------------------
One-shot helper: adds "Waiting For Approval" to the Status field's allowed
values on the `blogPost` content type, then republishes the content type so
the change takes effect.

Run once before using create_blog_entry.py with the new default status.

Reads the same env vars as create_blog_entry.py:
    CONTENTFUL_CMA_TOKEN, CONTENTFUL_SPACE_ID, CONTENTFUL_ENVIRONMENT,
    CONTENTFUL_CONTENT_TYPE (default 'blogPost')

Usage:
    python3 add_status_value.py
    python3 add_status_value.py --status-field status --new-value "Waiting For Approval"
"""

import argparse
import json
import os
import sys

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

CMA_BASE = f"https://api.contentful.com/spaces/{SPACE_ID}/environments/{ENV_ID}"
HEADERS = {
    "Authorization": f"Bearer {CMA_TOKEN}",
    "Content-Type": "application/vnd.contentful.management.v1+json",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--status-field", default="status",
                   help="API ID of the field on blogPost that holds status. Default 'status'.")
    p.add_argument("--new-value", default="Waiting For Approval",
                   help="Value to add to the dropdown. Default 'Waiting For Approval'.")
    args = p.parse_args()

    if not CMA_TOKEN or not SPACE_ID:
        sys.exit("CONTENTFUL_CMA_TOKEN and CONTENTFUL_SPACE_ID must be set.")

    # 1. Fetch the content type
    print(f"-> Fetching content type '{CONTENT_TYPE}'...")
    r = requests.get(f"{CMA_BASE}/content_types/{CONTENT_TYPE}",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    ct = r.json()
    version = ct["sys"]["version"]
    fields = ct["fields"]

    # 2. Locate the status field
    field = next((f for f in fields if f["id"] == args.status_field), None)
    if not field:
        sys.exit(f"No field with id '{args.status_field}' on '{CONTENT_TYPE}'. "
                 f"Available: {[f['id'] for f in fields]}")
    print(f"   Found field '{field['id']}' (type {field['type']})")

    # 3. Find or create the `in` validation
    validations = field.get("validations") or []
    in_val = next((v for v in validations if "in" in v), None)
    if in_val is None:
        print(f"   No `in` validation found. Creating one with value '{args.new_value}'.")
        validations.append({"in": [args.new_value]})
    else:
        existing = in_val["in"]
        if args.new_value in existing:
            print(f"   '{args.new_value}' already in allowed values: {existing}")
            print("   Nothing to do. Exiting.")
            return
        print(f"   Existing values: {existing}")
        in_val["in"] = existing + [args.new_value]
        print(f"   Updated values:  {in_val['in']}")
    field["validations"] = validations

    # 4. PUT the content type back
    print("-> Updating content type...")
    put = requests.put(
        f"{CMA_BASE}/content_types/{CONTENT_TYPE}",
        headers={**HEADERS, "X-Contentful-Version": str(version)},
        data=json.dumps({"name": ct["name"],
                         "displayField": ct.get("displayField"),
                         "description": ct.get("description"),
                         "fields": fields}),
        timeout=30,
    )
    if not put.ok:
        sys.exit(f"Update failed {put.status_code}: {put.text}")
    new_version = put.json()["sys"]["version"]
    print(f"   Content type updated (version {new_version}, draft).")

    # 5. Publish the content type so the change is live
    print("-> Publishing content type...")
    pub = requests.put(
        f"{CMA_BASE}/content_types/{CONTENT_TYPE}/published",
        headers={**HEADERS, "X-Contentful-Version": str(new_version)},
        timeout=30,
    )
    if not pub.ok:
        sys.exit(f"Publish failed {pub.status_code}: {pub.text}")
    print(f"   Published. '{args.new_value}' is now a valid Status value.\n")


if __name__ == "__main__":
    main()
