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

## Deploy on Railway

Railway runs the server over Streamable HTTP at `/mcp`. The repository includes
`railway.json`, which configures the start command and `/health` health check.

Set these Railway service variables before deploying:

```text
NOTION_API_KEY=your_notion_integration_secret
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}   # paste the whole key file's content
MEMBERS_JSON={"ghbot_<random>":"member name", ...}
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
