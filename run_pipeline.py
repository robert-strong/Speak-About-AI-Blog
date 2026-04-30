#!/usr/bin/env python3
"""
run_pipeline.py
---------------
End-to-end run for the scheduled task:
    1. draft_articles.py      (Brief -> markdown body + excerpt + meta)
    2. from_sheet.py --all    (push every Queued row to Contentful as draft)

Both steps update the queue sheet as they go. Nothing is published — entries
land as Drafts in Contentful with status 'Waiting For Approval' for human review.

Usage:
    python3 run_pipeline.py
    python3 run_pipeline.py --skip-draft        # only push existing Body Paths
    python3 run_pipeline.py --skip-publish      # only generate drafts
    python3 run_pipeline.py --dry-run           # show plan without writing
"""

import argparse
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).parent


def run(cmd, dry_run=False):
    print(f"\n>>> {' '.join(repr(a) for a in cmd)}")
    if dry_run:
        print("    (dry-run; not executing)")
        return 0
    proc = subprocess.run(cmd, cwd=str(HERE))
    return proc.returncode


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--skip-draft", action="store_true",
                   help="Skip the drafting step (only run from_sheet.py --all)")
    p.add_argument("--skip-publish", action="store_true",
                   help="Skip the publishing step (only run draft_articles.py)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    rc = 0

    if not args.skip_draft:
        print("=" * 60)
        print("STEP 1/2: Drafting articles from Briefs")
        print("=" * 60)
        cmd = [sys.executable, str(HERE / "draft_articles.py")]
        if args.dry_run:
            cmd.append("--dry-run")
        rc = run(cmd, dry_run=False) or rc
        if rc != 0:
            print(f"\nDrafting step exited with code {rc}.")

    if not args.skip_publish:
        print("\n" + "=" * 60)
        print("STEP 2/2: Pushing queued rows to Contentful")
        print("=" * 60)
        cmd = [sys.executable, str(HERE / "from_sheet.py"), "--all"]
        if args.dry_run:
            cmd.append("--dry-run")
        rc = run(cmd, dry_run=False) or rc
        if rc != 0:
            print(f"\nPublishing step exited with code {rc}.")

    print("\nPipeline complete.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
