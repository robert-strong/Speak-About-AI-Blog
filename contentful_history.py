#!/usr/bin/env python3
"""
contentful_history.py
---------------------
Pull the catalogue of blog posts that already exist in Contentful so the
brief generator can avoid repeating subjects the site has covered.

Uses the Content Management API (same CONTENTFUL_CMA_TOKEN the publish step
uses) rather than the Delivery API, so unpublished entries that are still
"Waiting for Approval" count as covered too. Otherwise the pipeline could
draft the same subject twice in consecutive months while the first draft sits
in review.

Required env (loaded by the caller from .env / .env.local or set externally):
    CONTENTFUL_CMA_TOKEN     Personal access token (CFPAT-...)
    CONTENTFUL_SPACE_ID      e.g. 2ssjs5z6qgs3
    CONTENTFUL_ENVIRONMENT   default 'master'
    CONTENTFUL_CONTENT_TYPE  default 'blogPost'
    CONTENTFUL_LOCALE        default 'en-US'

USAGE (standalone, for a quick look at what the generator will see)
    python3 contentful_history.py            # print the catalogue summary
    python3 contentful_history.py --block    # print the prompt block itself
"""

import argparse
import os
import re
import sys

import requests

CMA_TOKEN = (os.environ.get("CONTENTFUL_CMA_TOKEN")
             or os.environ.get("CONTENTFUL_MANAGEMENT_TOKEN"))
SPACE_ID = os.environ.get("CONTENTFUL_SPACE_ID")
ENV_ID = os.environ.get("CONTENTFUL_ENVIRONMENT", "master")
CONTENT_TYPE = os.environ.get("CONTENTFUL_CONTENT_TYPE", "blogPost")
LOCALE = os.environ.get("CONTENTFUL_LOCALE", "en-US")

# Fields we pull. Keep this narrow: the body is large and not needed here.
_SELECT = ",".join([
    "sys.id", "sys.publishedAt", "sys.archivedAt", "sys.createdAt",
    "fields.title", "fields.slug", "fields.excerpt", "fields.metaDescription",
    "fields.tags", "fields.seoKeywords", "fields.category",
    "fields.publishedDate",
])

# Words that appear in almost every title on the site and therefore say
# nothing about the subject. Kept out of the overlap score so that two
# unrelated "AI keynote speaker" articles are not flagged as the same topic.
_DOMAIN_STOPWORDS = {
    "ai", "keynote", "keynotes", "speaker", "speakers", "event", "events",
    "guide", "best", "top", "2024", "2025", "2026", "2027", "how", "why",
    "what", "when", "your", "you", "the", "and", "for", "with", "from",
    "into", "that", "this", "than", "then", "them", "they", "their", "are",
    "not", "but", "can", "will", "should", "need", "needs", "now", "before",
    "after", "about", "who", "which", "ways", "tips", "vs", "versus",
    "complete", "ultimate", "every", "actually", "really", "one", "two",
    "three", "four", "five", "ten", "first", "new", "more", "most", "here",
    "there", "have", "has", "had", "does", "did", "get", "gets", "make",
    "makes", "use", "using", "used", "its", "our", "out", "off", "all",
    "any", "own", "just", "also", "over", "under", "between", "without",
    "still", "while", "where", "each", "per",
}


def _loc(fields, key):
    """Return the localized value of a field from a CMA entry, or None."""
    value = fields.get(key)
    if isinstance(value, dict):
        return value.get(LOCALE) or next(iter(value.values()), None)
    return value


def fetch_published_topics(page_size=100, max_entries=2000):
    """Return every non-archived blog post as a list of small dicts.

    Newest first. Includes unpublished drafts (live=False) on purpose; see
    the module docstring. Returns [] when Contentful is not configured so
    callers can degrade gracefully.
    """
    if not CMA_TOKEN or not SPACE_ID:
        return []

    base = f"https://api.contentful.com/spaces/{SPACE_ID}/environments/{ENV_ID}/entries"
    headers = {"Authorization": f"Bearer {CMA_TOKEN}"}
    topics = []
    skip = 0
    while skip < max_entries:
        r = requests.get(base, headers=headers, timeout=30, params={
            "content_type": CONTENT_TYPE,
            "select": _SELECT,
            "limit": page_size,
            "skip": skip,
            "order": "-sys.createdAt",
        })
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for entry in items:
            sys_ = entry.get("sys", {})
            if sys_.get("archivedAt"):
                continue
            f = entry.get("fields", {})
            title = (_loc(f, "title") or "").strip()
            if not title:
                continue
            tags = _loc(f, "tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            keywords = _loc(f, "seoKeywords") or ""
            if isinstance(keywords, list):
                keywords = ", ".join(str(k) for k in keywords)
            topics.append({
                "id": sys_.get("id"),
                "title": title,
                "slug": (_loc(f, "slug") or "").strip(),
                "excerpt": (_loc(f, "excerpt") or _loc(f, "metaDescription") or "").strip(),
                "tags": [str(t) for t in tags],
                "keywords": str(keywords).strip(),
                "category": _loc(f, "category") or "",
                "date": (_loc(f, "publishedDate") or sys_.get("createdAt") or "")[:10],
                "live": bool(sys_.get("publishedAt")),
            })
        skip += len(items)
        if not items or skip >= data.get("total", 0):
            break

    topics.sort(key=lambda t: t["date"], reverse=True)
    return topics


def format_history_block(topics, max_chars=30000):
    """Render the catalogue as a compact list for the generation prompt.

    Newest articles get a one-line summary (title, slug, excerpt, tags).
    Once the character budget is roughly two-thirds used, older articles
    fall back to titles only so the whole catalogue still fits.
    """
    if not topics:
        return "(no published articles found)"

    lines = []
    used = 0
    detail_budget = int(max_chars * 0.66)
    for t in topics:
        excerpt = re.sub(r"\s+", " ", t["excerpt"])[:160]
        tags = ", ".join(t["tags"][:6])
        status = "" if t["live"] else " [draft, awaiting approval]"
        if used < detail_budget:
            line = f"- {t['title']}{status} (/{t['slug']})"
            if excerpt:
                line += f" — {excerpt}"
            if tags:
                line += f" [tags: {tags}]"
        else:
            line = f"- {t['title']}{status}"
        if used + len(line) + 1 > max_chars:
            remaining = len(topics) - len(lines)
            lines.append(f"- ...and {remaining} more older articles not listed")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def content_words(text):
    """Lowercase alphanumeric tokens with generic and domain stopwords removed."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _DOMAIN_STOPWORDS}


def lexical_overlap(brief, topic):
    """Score how much of a topic's title vocabulary a brief reuses (0..1).

    Cheap and deterministic. It is a signal for the run log, not the final
    word: the Claude judge in generate_briefs.py makes the drop decision.
    """
    title_words = content_words(topic["title"])
    if len(title_words) < 2:
        title_words |= content_words(" ".join(topic["tags"]))
    if not title_words:
        return 0.0
    brief_words = content_words(brief)
    return len(title_words & brief_words) / len(title_words)


def closest_matches(brief, topics, top_n=3, min_score=0.0):
    """Return [(score, topic), ...] for the topics most similar to a brief."""
    scored = [(lexical_overlap(brief, t), t) for t in topics]
    scored = [s for s in scored if s[0] > min_score]
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored[:top_n]


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
        load_dotenv(".env.local", override=True)
    except ImportError:
        pass
    # Re-read env after dotenv so standalone runs pick up the local file.
    global CMA_TOKEN, SPACE_ID, ENV_ID
    CMA_TOKEN = (os.environ.get("CONTENTFUL_CMA_TOKEN")
                 or os.environ.get("CONTENTFUL_MANAGEMENT_TOKEN"))
    SPACE_ID = os.environ.get("CONTENTFUL_SPACE_ID")
    ENV_ID = os.environ.get("CONTENTFUL_ENVIRONMENT", "master")

    p = argparse.ArgumentParser(description="Inspect the Contentful blog catalogue.")
    p.add_argument("--block", action="store_true", help="Print the prompt block instead of a summary.")
    args = p.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not CMA_TOKEN or not SPACE_ID:
        sys.exit("CONTENTFUL_CMA_TOKEN and CONTENTFUL_SPACE_ID are required.")

    topics = fetch_published_topics()
    if args.block:
        print(format_history_block(topics))
        return
    live = sum(1 for t in topics if t["live"])
    print(f"{len(topics)} blog posts in Contentful ({live} published, {len(topics) - live} drafts)")
    for t in topics:
        flag = "" if t["live"] else "  [draft]"
        print(f"  {t['date']}  {t['title']}{flag}")


if __name__ == "__main__":
    main()
