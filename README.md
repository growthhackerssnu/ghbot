# ghbot

Growth Hackers Notion + Google Drive + Slack MCP server.

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
`SUPABASE_KEY` or `SUPABASE_SECRET_KEY`; the publishable key is not accepted for
index access after RLS is enabled. Never commit any Supabase key.

### Daily sync

The repository includes [`sync_worker.py`](sync_worker.py), a short-lived job
that hydrates the staging index from Supabase, incrementally fetches changed
Notion project pages and Tasks/meeting notes, publishes the result, then
syncs Slack (see below) and exits.
In production it runs as the Cloud Run Job `ghbot-sync`, using the same image as
the web service, and Cloud Scheduler starts it daily at 03:00 KST. The deploy
workflow keeps the job's image up to date. The job and scheduler need this
one-time setup:

```bash
REGION=<region>; PROJECT=<project-id>; SA=ghbot-run@$PROJECT.iam.gserviceaccount.com
IMAGE=$(gcloud run services describe ghbot --region=$REGION --format='value(spec.template.spec.containers[0].image)')

# Sync needs Notion, Supabase and the Slack token key (Drive is not embedded).
gcloud run jobs create ghbot-sync --region=$REGION --image=$IMAGE \
  --command=python --args=sync_worker.py \
  --service-account=$SA --memory=2Gi --task-timeout=7200 --max-retries=0 \
  --set-env-vars=SUPABASE_URL=https://<ghbot-project>.supabase.co \
  --set-secrets=NOTION_API_KEY=GHBOT_NOTION_API_KEY:latest,SUPABASE_KEY=GHBOT_SUPABASE_KEY:latest,SLACK_TOKEN_ENCRYPTION_KEY=GHBOT_SLACK_TOKEN_ENCRYPTION_KEY:latest

gcloud run jobs add-iam-policy-binding ghbot-sync --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker

gcloud scheduler jobs create http ghbot-sync-daily --location=$REGION \
  --schedule="0 3 * * *" --time-zone="Asia/Seoul" --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/ghbot-sync:run" \
  --oauth-service-account-email=$SA
```

Run it by hand with `gcloud run jobs execute ghbot-sync --region=$REGION`. The
first run can instead use `python sync_worker.py --upload-only` locally to
publish an existing `gh_bot.db`.

### Slack

Slack has no bot to invite. Each member connects their own Slack account once
(ask Claude to "connect Slack" - the `connect_slack` tool returns the link),
and ghbot stores that user token encrypted in `ghbot_slack_users`.

- **Sync:** the nightly job lists every connected member's conversations
  (public, private, group DMs, DMs), crawls each one once, and stores threads
  and per-day message groups as embedded chunks in `ghbot_slack_chunks`. The
  last 14 days are re-read each night for edits, deletions and new replies.
  The first backfill stops after `SLACK_SYNC_BUDGET_SECONDS` (default 3000)
  and resumes the next night; for a one-off long run use
  `gcloud run jobs execute ghbot-sync --args=sync_worker.py,--slack-only,--slack-budget=20000 --task-timeout=21600`.
- **Access:** `search_slack` asks Slack, with the caller's own token, which
  conversations they are in right now, and the database function
  `ghbot_match_slack` only returns chunks from those (plus public channels for
  non-guests). A DM between A and B is searchable by A and B only.
  `fetch_slack_thread` reads threads live with the caller's token.

Setup: create the app from [`slack_app_manifest.yml`](slack_app_manifest.yml)
at api.slack.com/apps, rerun [`supabase_schema.sql`](supabase_schema.sql),
then set `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_TEAM_ID` and
`SLACK_TOKEN_ENCRYPTION_KEY` on the service, and `SLACK_TOKEN_ENCRYPTION_KEY`
on the job.

## Deploy on Cloud Run

Pushes to `main` build the image and deploy the `ghbot` Cloud Run service via
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml). The server
runs over Streamable HTTP at `/mcp`, with a `/health` check. Environment
variables are set on the Cloud Run service directly; the workflow only swaps
the image:

```text
NOTION_API_KEY=your_notion_integration_secret
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}   # paste the whole key file's content
MEMBERS_JSON={"ghbot_<random>":"member name", ...}
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-private-backend-key
```

### Admin-managed member tokens

`MEMBERS_JSON` remains supported for existing static tokens. To validate tokens
issued by the admin service without exposing the admin Supabase database to
Cloud Run, set the following on the ghbot service:

```text
ADMIN_API_URL=https://<admin-api-domain>
GHBOT_AUTH_SHARED_SECRET=<same random value configured on the admin backend>
```

The ghbot server sends a token or token hash only to the admin backend's
internal verification endpoint. The backend compares its SHA-256 hash against
an active acting-member token, so a revoked token stops working immediately.

`MEMBERS_JSON` is required for the remote deployment - the server refuses to
start over HTTP with no members configured rather than serving real Notion/Drive
data to anyone who finds the URL. Generate member tokens locally with:

```bash
python manage_members.py add "member name"
```

then copy the resulting `{token: name}` pairs into `MEMBERS_JSON` (a plain
`members.json` file also works for local runs, but Cloud Run has no persistent/
committed filesystem for secrets, hence the env var form).

Connect your MCP client to:

```text
https://api.ghsnu.com/mcp
```

with an `Authorization: Bearer <member token>` header (Claude and ChatGPT
custom connectors both support a static bearer token/API key for remote MCP
servers - no OAuth server needed).
