# ghbot

Growth Hackers Notion MCP server.

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

Set this Railway service variable before deploying:

```text
NOTION_API_KEY=your_notion_integration_secret
```

After Railway generates a public domain, connect your MCP client to:

```text
https://your-domain.up.railway.app/mcp
```
