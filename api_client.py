#!/usr/bin/env python3
"""
api_client.py
-------------
REST API client for the SpeakAbout.AI blog pipeline.
Replaces Google Sheets integration with database-backed API.

This module provides functions to:
- Get queued items from the blog queue
- Update queue item status and fields
- Get existing briefs for de-duplication
- Create new briefs in the queue
- Get pipeline settings (prompts, ratios)

ENVIRONMENT VARIABLES
---------------------
BLOG_API_BASE        — Base URL for the API (e.g., https://speakabout.ai/api/blog-pipeline)
BLOG_PIPELINE_API_KEY — API key for authentication (Bearer token)

USAGE
-----
    from api_client import BlogPipelineAPI

    api = BlogPipelineAPI()

    # Get existing briefs for de-duplication
    briefs = api.get_existing_briefs(limit=30)

    # Create new briefs
    api.create_briefs(["Brief 1", "Brief 2", "Brief 3"])

    # Get queued items
    items = api.get_queued_items()

    # Update an item
    api.update_item(item_id, status="drafted", title="My Title", body_content="...")

    # Get a setting
    prompt = api.get_setting("briefs_prompt")
"""

import os
import sys
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)
except ImportError:
    pass

import requests


class BlogPipelineAPI:
    """REST API client for the blog pipeline."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 60
    ):
        self.base_url = base_url or os.environ.get(
            "BLOG_API_BASE",
            "https://speakabout.ai/api/blog-pipeline"
        )
        self.api_key = api_key or os.environ.get("BLOG_PIPELINE_API_KEY")
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "BLOG_PIPELINE_API_KEY environment variable is required. "
                "Set it in .env.local or pass api_key to the constructor."
            )

    def _headers(self) -> dict:
        """Return authorization headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None
    ) -> dict:
        """Make an API request and return the JSON response."""
        url = f"{self.base_url}{endpoint}"

        r = requests.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json,
            timeout=self.timeout
        )

        if not r.ok:
            error_msg = f"API error {r.status_code}: {r.text[:500]}"
            raise RuntimeError(error_msg)

        return r.json()

    # ========== BRIEFS ==========

    def get_existing_briefs(self, limit: int = 30) -> list[str]:
        """
        Get existing briefs for de-duplication context.

        Args:
            limit: Maximum number of briefs to return (most recent first)

        Returns:
            List of brief strings
        """
        data = self._request("GET", "/briefs", params={"limit": limit})
        return data.get("briefs", [])

    def create_briefs(self, briefs: list[str]) -> list[dict]:
        """
        Create new brief items in the queue.

        Args:
            briefs: List of brief strings to add

        Returns:
            List of created queue item objects
        """
        data = self._request("POST", "/briefs", json={"briefs": briefs})
        return data.get("items", [])

    # ========== QUEUE ==========

    def get_queued_items(self) -> list[dict]:
        """
        Get all items with status 'queued'.

        Returns:
            List of queue item objects
        """
        data = self._request("GET", "/queue")
        return data.get("items", [])

    def get_item(self, item_id: int) -> dict:
        """
        Get a single queue item by ID.

        Args:
            item_id: The queue item ID

        Returns:
            Queue item object
        """
        data = self._request("GET", "/queue", params={"id": item_id})
        return data.get("item", {})

    def update_item(self, item_id: int, **fields) -> dict:
        """
        Update a queue item.

        Args:
            item_id: The queue item ID
            **fields: Fields to update (status, title, slug, body_content, etc.)

        Returns:
            Updated queue item object
        """
        data = self._request("PUT", "/queue", json={"id": item_id, **fields})
        return data.get("item", {})

    # ========== SETTINGS ==========

    def get_setting(self, key: str) -> Optional[str]:
        """
        Get a pipeline setting value.

        Args:
            key: Setting key (e.g., "briefs_prompt", "cta_ratio")

        Returns:
            Setting value as string, or None if not found
        """
        try:
            data = self._request("GET", "/settings", params={"key": key})
            return data.get("value")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise


# ========== CONVENIENCE FUNCTIONS ==========

_default_api: Optional[BlogPipelineAPI] = None


def _get_api() -> BlogPipelineAPI:
    """Get or create the default API client."""
    global _default_api
    if _default_api is None:
        _default_api = BlogPipelineAPI()
    return _default_api


def get_existing_briefs(limit: int = 30) -> list[str]:
    """Get existing briefs for de-duplication (uses default API client)."""
    return _get_api().get_existing_briefs(limit)


def create_briefs(briefs: list[str]) -> list[dict]:
    """Create new briefs in the queue (uses default API client)."""
    return _get_api().create_briefs(briefs)


def get_queued_items() -> list[dict]:
    """Get queued items (uses default API client)."""
    return _get_api().get_queued_items()


def get_item(item_id: int) -> dict:
    """Get a single queue item (uses default API client)."""
    return _get_api().get_item(item_id)


def update_item(item_id: int, **fields) -> dict:
    """Update a queue item (uses default API client)."""
    return _get_api().update_item(item_id, **fields)


def get_setting(key: str) -> Optional[str]:
    """Get a setting value (uses default API client)."""
    return _get_api().get_setting(key)


# ========== COMPATIBILITY LAYER ==========
# These functions provide backward-compatible interfaces that match
# the Google Sheets-based functions in the original scripts.


def get_headers_and_rows() -> tuple[list[str], list[dict]]:
    """
    Get queue items in a format similar to the Google Sheets read_rows().

    Returns:
        (headers, rows) where headers is a list of column names and
        rows is a list of dicts with '__row__' set to item ID.
    """
    items = get_queued_items()

    headers = [
        "Status", "Brief", "Title", "Slug", "Excerpt", "Meta Description",
        "Body Path", "Body Content", "Image Prompt", "Hero Image URL",
        "Category", "Tags", "SEO Keywords", "Published Date", "Display Title",
        "Speakers", "Author ID", "Entry URL", "Contentful Entry ID",
        "Last Run", "Notes", "Error Message"
    ]

    # Convert API items to sheet-like rows
    field_map = {
        "Status": "status",
        "Brief": "brief",
        "Title": "title",
        "Slug": "slug",
        "Excerpt": "excerpt",
        "Meta Description": "meta_description",
        "Body Path": "body_path",
        "Body Content": "body_content",
        "Image Prompt": "image_prompt",
        "Hero Image URL": "hero_image_url",
        "Category": "category",
        "Tags": "tags",
        "SEO Keywords": "seo_keywords",
        "Published Date": "published_date",
        "Display Title": "display_title",
        "Speakers": "speakers",
        "Author ID": "author_id",
        "Entry URL": "contentful_entry_url",
        "Contentful Entry ID": "contentful_entry_id",
        "Last Run": "last_run",
        "Notes": "notes",
        "Error Message": "error_message",
    }

    rows = []
    for item in items:
        row = {"__row__": item["id"]}
        for header, field in field_map.items():
            value = item.get(field)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            row[header] = str(value) if value is not None else ""
        rows.append(row)

    return headers, rows


def update_row_field(row_id: int, field_name: str, value: Any) -> None:
    """
    Update a single field on a queue item (sheet-style interface).

    Args:
        row_id: The item ID (was row number in sheets)
        field_name: The header/field name to update
        value: The new value
    """
    # Map sheet-style field names to API field names
    field_map = {
        "Status": "status",
        "Brief": "brief",
        "Title": "title",
        "Slug": "slug",
        "Excerpt": "excerpt",
        "Meta Description": "meta_description",
        "Body Path": "body_path",
        "Body Content": "body_content",
        "Image Prompt": "image_prompt",
        "Hero Image URL": "hero_image_url",
        "Category": "category",
        "Tags": "tags",
        "SEO Keywords": "seo_keywords",
        "Published Date": "published_date",
        "Display Title": "display_title",
        "Speakers": "speakers",
        "Author ID": "author_id",
        "Entry URL": "contentful_entry_url",
        "Contentful Entry ID": "contentful_entry_id",
        "Last Run": "last_run",
        "Notes": "notes",
        "Error Message": "error_message",
    }

    api_field = field_map.get(field_name)
    if not api_field:
        print(f"Warning: Unknown field '{field_name}', skipping update",
              file=sys.stderr)
        return

    update_item(row_id, **{api_field: value})


# ========== MAIN (for testing) ==========

if __name__ == "__main__":
    import json

    print("Testing Blog Pipeline API client...")
    print(f"API Base URL: {os.environ.get('BLOG_API_BASE', '(default)')}")

    try:
        api = BlogPipelineAPI()

        # Test getting briefs
        print("\n1. Getting existing briefs...")
        briefs = api.get_existing_briefs(limit=5)
        print(f"   Found {len(briefs)} briefs")
        if briefs:
            print(f"   First brief: {briefs[0][:100]}...")

        # Test getting settings
        print("\n2. Getting settings...")
        cta_ratio = api.get_setting("cta_ratio")
        print(f"   CTA ratio: {cta_ratio}")

        # Test getting queue
        print("\n3. Getting queued items...")
        items = api.get_queued_items()
        print(f"   Found {len(items)} queued items")
        if items:
            print(f"   First item ID: {items[0]['id']}")

        print("\n✓ All tests passed!")

    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)
