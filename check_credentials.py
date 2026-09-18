#!/usr/bin/env python3
"""
check_credentials.py
--------------------
Verify every credential the blog pipeline depends on BEFORE a run touches
anything. A stale secret then fails in seconds with a message that says
exactly which secret to rotate, instead of failing mid-publish after images
have been generated and queue items have been flipped to "error".

Runs as the first step of every pipeline workflow and on a weekly schedule
(see .github/workflows/credentials-health.yml). With --notify, failures are
also emailed to the site admins through the website's alert endpoint.

USAGE
    python3 check_credentials.py                       # check whatever is configured
    python3 check_credentials.py --require contentful,blog_api
    python3 check_credentials.py --all --notify        # weekly health check

Checks (each only runs when its env vars are present, unless required):
    contentful   CONTENTFUL_CMA_TOKEN + CONTENTFUL_SPACE_ID against both the
                 Management API and the Upload API (they can disagree)
    anthropic    ANTHROPIC_API_KEY
    blog_api     BLOG_API_BASE + BLOG_PIPELINE_API_KEY
    google_ai    GOOGLE_AI_API_KEY (hero image generation)

Exit status is 1 when any *required* check fails; optional checks that fail
print a warning only.
"""

import argparse
import json
import os
import sys

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)
except ImportError:
    pass

TIMEOUT = 20
SECRETS_PAGE = "GitHub repo -> Settings -> Secrets and variables -> Actions"


class Result:
    def __init__(self, name, ok, detail, fix=None, skipped=False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fix = fix
        self.skipped = skipped


# --------------------------------------------------------------------------
# Individual checks. Each returns a Result and never raises.
# --------------------------------------------------------------------------

def check_contentful():
    token = os.environ.get("CONTENTFUL_CMA_TOKEN") or os.environ.get("CONTENTFUL_MANAGEMENT_TOKEN")
    space = os.environ.get("CONTENTFUL_SPACE_ID")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT", "master")
    if not token or not space:
        return Result("contentful", False, "CONTENTFUL_CMA_TOKEN or CONTENTFUL_SPACE_ID not set",
                      fix=f"Add both secrets under {SECRETS_PAGE}.", skipped=True)
    headers = {"Authorization": f"Bearer {token}"}
    fix = ("Create a new Contentful personal access token (Contentful -> Settings -> "
           "CMA tokens -> Generate personal token) and paste it into the "
           f"CONTENTFUL_CMA_TOKEN secret under {SECRETS_PAGE}. Update Blog/.env.local "
           "with the same value so local runs keep working.")
    try:
        r = requests.get(
            f"https://api.contentful.com/spaces/{space}/environments/{env_id}/entries",
            headers=headers, params={"content_type": "blogPost", "limit": 1}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return Result("contentful", False, f"Management API unreachable: {e}")
    if r.status_code in (401, 403):
        return Result("contentful", False,
                      f"Management API rejected the token (HTTP {r.status_code})", fix=fix)
    if r.status_code == 404:
        return Result("contentful", False,
                      f"Space {space!r} / environment {env_id!r} not found (HTTP 404)",
                      fix="Check CONTENTFUL_SPACE_ID and CONTENTFUL_ENVIRONMENT secrets.")
    if not r.ok:
        return Result("contentful", False, f"Management API HTTP {r.status_code}: {r.text[:200]}")
    total = r.json().get("total")

    # The Upload API is a separate host and is where the September 2026
    # publish run failed, so probe it explicitly. A 404 for a made-up upload
    # id proves the token was accepted; 401/403 means it was not.
    try:
        u = requests.get(f"https://upload.contentful.com/spaces/{space}/uploads/credential-check",
                         headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        return Result("contentful", False, f"Upload API unreachable: {e}")
    if u.status_code in (401, 403):
        return Result("contentful", False,
                      f"Upload API rejected the token (HTTP {u.status_code}) even though the "
                      "Management API accepted it", fix=fix)
    if u.status_code not in (404, 200):
        return Result("contentful", False, f"Upload API HTTP {u.status_code}: {u.text[:200]}")
    return Result("contentful", True,
                  f"space {space}, env {env_id}, {total} blog posts; upload API OK")


def check_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return Result("anthropic", False, "ANTHROPIC_API_KEY not set",
                      fix=f"Add the secret under {SECRETS_PAGE}.", skipped=True)
    try:
        r = requests.get("https://api.anthropic.com/v1/models", timeout=TIMEOUT,
                         params={"limit": 1},
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except requests.RequestException as e:
        return Result("anthropic", False, f"API unreachable: {e}")
    if r.status_code in (401, 403):
        return Result("anthropic", False, f"API key rejected (HTTP {r.status_code})",
                      fix="Create a new key at console.anthropic.com -> API keys and update "
                          f"the ANTHROPIC_API_KEY secret under {SECRETS_PAGE}.")
    if not r.ok:
        return Result("anthropic", False, f"HTTP {r.status_code}: {r.text[:200]}")
    return Result("anthropic", True, "API key accepted")


def check_blog_api():
    base = os.environ.get("BLOG_API_BASE", "https://speakabout.ai/api/blog-pipeline").rstrip("/")
    key = os.environ.get("BLOG_PIPELINE_API_KEY")
    if not key:
        return Result("blog_api", False, "BLOG_PIPELINE_API_KEY not set",
                      fix=f"Add the secret under {SECRETS_PAGE}; it must match the "
                          "BLOG_PIPELINE_API_KEY env var in Vercel.", skipped=True)
    try:
        r = requests.get(f"{base}/briefs", params={"limit": 1}, timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {key}"})
    except requests.RequestException as e:
        return Result("blog_api", False, f"{base} unreachable: {e}")
    if r.status_code in (401, 403):
        return Result("blog_api", False, f"API key rejected by {base} (HTTP {r.status_code})",
                      fix="The BLOG_PIPELINE_API_KEY secret in GitHub must equal the "
                          "BLOG_PIPELINE_API_KEY environment variable in Vercel. Update "
                          "whichever side changed and redeploy the site if Vercel did.")
    if not r.ok:
        return Result("blog_api", False, f"HTTP {r.status_code} from {base}: {r.text[:200]}")
    return Result("blog_api", True, f"{base} accepted the key")


def check_google_ai():
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        return Result("google_ai", False, "GOOGLE_AI_API_KEY not set",
                      fix=f"Add the secret under {SECRETS_PAGE}.", skipped=True)
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 1}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return Result("google_ai", False, f"API unreachable: {e}")
    if r.status_code in (400, 401, 403):
        return Result("google_ai", False, f"API key rejected (HTTP {r.status_code})",
                      fix="Create a new key in Google AI Studio and update the "
                          f"GOOGLE_AI_API_KEY secret under {SECRETS_PAGE}.")
    if not r.ok:
        return Result("google_ai", False, f"HTTP {r.status_code}: {r.text[:200]}")
    return Result("google_ai", True, "API key accepted")


CHECKS = {
    "contentful": check_contentful,
    "anthropic": check_anthropic,
    "blog_api": check_blog_api,
    "google_ai": check_google_ai,
}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _run_context():
    """Describe the GitHub Actions run, if we are inside one."""
    workflow = os.environ.get("GITHUB_WORKFLOW")
    if not workflow:
        return "local run", None
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "?")
    url = None
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_REPOSITORY") \
            and os.environ.get("GITHUB_RUN_ID"):
        url = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return f"{workflow} #{run_number}", url


def _write_step_summary(results, required):
    """Add a table to the GitHub Actions job summary when available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = ["| Credential | Status | Detail |", "|---|---|---|"]
    for r in results:
        if r.ok:
            status = "✅ OK"
        elif r.name in required:
            status = "❌ FAIL"
        else:
            status = "⚠️ warn"
        rows.append(f"| {r.name} | {status} | {r.detail} |")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("### Credential check\n\n" + "\n".join(rows) + "\n\n")
    except OSError:
        pass


def notify(failures, required):
    """Email the admins through the website's alert endpoint. Best effort."""
    base = os.environ.get("BLOG_API_BASE", "https://speakabout.ai/api/blog-pipeline").rstrip("/")
    key = os.environ.get("BLOG_PIPELINE_API_KEY")
    if not key:
        print("   (cannot notify: BLOG_PIPELINE_API_KEY not set)")
        return False
    context, url = _run_context()
    fatal = [f for f in failures if f.name in required]
    subject = (f"Blog pipeline blocked: {', '.join(f.name for f in fatal)} credential failed"
               if fatal else
               f"Blog pipeline warning: {', '.join(f.name for f in failures)} credential failed")
    payload = {
        "source": context,
        "run_url": url,
        "subject": subject,
        "failures": [
            {"name": f.name, "detail": f.detail, "fix": f.fix or "", "fatal": f.name in required}
            for f in failures
        ],
    }
    try:
        r = requests.post(f"{base}/alert", json=payload, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}"})
    except requests.RequestException as e:
        print(f"   (alert could not be sent: {e})")
        return False
    if r.ok:
        print(f"   Alert sent: {r.json().get('summary', 'ok')}")
        return True
    print(f"   (alert endpoint returned HTTP {r.status_code}: {r.text[:200]})")
    return False


def main():
    p = argparse.ArgumentParser(description="Verify the blog pipeline's credentials.")
    p.add_argument("--require", default="",
                   help="Comma-separated checks that must pass (default: none are fatal; "
                        "everything configured is checked and reported).")
    p.add_argument("--all", action="store_true",
                   help="Require every check, including ones whose env vars are missing.")
    p.add_argument("--notify", action="store_true",
                   help="Email admins via the website's /alert endpoint when a check fails.")
    args = p.parse_args()

    required = set(CHECKS) if args.all else {n.strip() for n in args.require.split(",") if n.strip()}
    unknown = required - set(CHECKS)
    if unknown:
        sys.exit(f"Unknown check(s): {', '.join(sorted(unknown))}. Known: {', '.join(CHECKS)}")

    context, _ = _run_context()
    print(f"Credential check ({context})")
    results = []
    for name, fn in CHECKS.items():
        result = fn()
        if result.skipped and name not in required:
            print(f"   -    {name:<11} not configured, skipped")
            continue
        results.append(result)
        if result.ok:
            print(f"   OK   {name:<11} {result.detail}")
        else:
            level = "FAIL" if name in required else "WARN"
            print(f"   {level} {name:<11} {result.detail}")
            if result.fix:
                print(f"        fix: {result.fix}")
            if os.environ.get("GITHUB_ACTIONS"):
                kind = "error" if name in required else "warning"
                print(f"::{kind}::{name} credential check failed: {result.detail}")

    _write_step_summary(results, required)
    failures = [r for r in results if not r.ok]
    if failures and args.notify:
        notify(failures, required)

    fatal = [r for r in failures if r.name in required]
    if fatal:
        print(f"\n{len(fatal)} required credential(s) failed; stopping before the pipeline runs.")
        sys.exit(1)
    print("\nAll required credentials OK.")


if __name__ == "__main__":
    main()
