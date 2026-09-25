"""One-shot Notion/Drive -> Supabase sync worker.

This process is intentionally short-lived so it can run as a Railway Cron
service. It incrementally rebuilds the local staging index, publishes the
result to Supabase, and exits. The MCP web service never needs to share or
mutate the staging SQLite file.
"""

from __future__ import annotations

import argparse
import json

from embed_index import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Notion and Google Drive into Supabase")
    parser.add_argument("--full", action="store_true", help="re-fetch and re-embed every document")
    parser.add_argument("--upload-only", action="store_true", help="publish the existing gh_bot.db without fetching")
    parser.add_argument("--notion-only", action="store_true")
    parser.add_argument("--drive-only", action="store_true")
    args = parser.parse_args()

    summary = run(
        full_rebuild=args.full,
        upload_only=args.upload_only,
        skip_notion=args.drive_only,
        skip_drive=args.notion_only,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
