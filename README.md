# ghbot

Growth Hackers Notion + Google Drive MCP server.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env
python server.py
```

The default local transport is stdio. This is the mode used by Claude Code,
Codex, and other clients that launch the server as a subprocess.

The semantic index is read directly from Supabase Postgres. Run
[`supabase_schema.sql`](supabase_schema.sql) once in the Supabase SQL Editor,
set `SUPABASE_URL` and `SUPABASE_KEY`, then publish the existing local index
with:

```bash
python3 embed_index.py --upload-only
```

`gh_bot.db` is only a local build/staging database now; the MCP server does not
use it at runtime. The MCP server and sync worker require private
`SUPABASE_KEY`; the `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` is not accepted for
index access after RLS is enabled. Never commit any Supabase key.

### Daily sync

The repository includes [`sync_worker.py`](sync_worker.py), a short-lived job
that hydrates the staging index from Supabase, incrementally fetches changed
Notion/Drive documents, publishes the result, and exits. Create a separate
Railway service from this repository for the worker; keep the existing web
service on `railway.json`. Use [`railway.sync.json`](railway.sync.json) as the
worker service configuration, or set these values in Railway's service
settings:

```text
Start command: python sync_worker.py
Cron schedule: 0 18 * * *
```

Railway schedules in UTC, so this runs daily at 03:00 KST. Copy the same
`NOTION_API_KEY`, Google credentials, and private `SUPABASE_URL`/
`SUPABASE_KEY` variables to the worker service. The first run can instead use
`python sync_worker.py --upload-only` to publish an existing `gh_bot.db`.

## Deploy on Railway

Railway runs the server over Streamable HTTP at `/mcp`. The repository includes
`railway.json`, which configures the start command and `/health` health check.

Set these Railway service variables before deploying:

```text
NOTION_API_KEY=your_notion_integration_secret
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}   # paste the whole key file's content
MEMBERS_JSON={"ghbot_<random>":"member name", ...}
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-private-backend-key
```

`MEMBERS_JSON` is required for the remote deployment - the server refuses to
start over HTTP with no members configured rather than serving real Notion/Drive
data to anyone who finds the URL. Generate member tokens locally with:

```bash
python manage_members.py add "member name"
```

then copy the resulting `{token: name}` pairs into `MEMBERS_JSON` (a plain
`members.json` file also works for local runs, but Railway has no persistent/
committed filesystem for secrets, hence the env var form).

After Railway generates a public domain, connect your MCP client to:

```text
https://your-domain.up.railway.app/mcp
```

with an `Authorization: Bearer <member token>` header (Claude and ChatGPT
custom connectors both support a static bearer token/API key for remote MCP
servers - no OAuth server needed).
