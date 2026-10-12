"""One-shot sync worker: Notion and Slack -> Supabase semantic index.

This process is intentionally short-lived so it can run as a Cloud Run Job
triggered daily by Cloud Scheduler. It incrementally rebuilds the local
staging index, publishes the result to Supabase, and exits. The MCP web
service never needs to share or mutate the staging SQLite file. Slack is
written to Supabase directly (slack_index.py); a failure there does not undo
the Notion publish.
"""

from __future__ import annotations

import argparse
import json

from embed_index import run
from supabase_store import SupabaseStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Notion and Slack semantic indexes into Supabase")
    parser.add_argument("--full", action="store_true", help="re-fetch and re-embed every Notion document")
    parser.add_argument("--upload-only", action="store_true", help="publish the existing gh_bot.db without fetching")
    parser.add_argument("--skip-slack", action="store_true", help="only sync Notion")
    parser.add_argument("--slack-only", action="store_true", help="only sync Slack")
    parser.add_argument("--slack-budget", type=float, help="seconds Slack may spend before resuming next run")
    args = parser.parse_args()

    summary = {}
    if not args.slack_only:
        summary["notion"] = run(
            full_rebuild=args.full,
            upload_only=args.upload_only,
        )
    if not args.skip_slack and not args.upload_only:
        import slack_index

        summary["slack"] = slack_index.run(SupabaseStore(), budget_seconds=args.slack_budget)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
