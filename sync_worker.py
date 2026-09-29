"""One-shot Notion -> Supabase semantic-index sync worker.

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
    parser = argparse.ArgumentParser(description="Sync Notion semantic index into Supabase")
    parser.add_argument("--full", action="store_true", help="re-fetch and re-embed every document")
    parser.add_argument("--upload-only", action="store_true", help="publish the existing gh_bot.db without fetching")
    args = parser.parse_args()

    summary = run(
        full_rebuild=args.full,
        upload_only=args.upload_only,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
