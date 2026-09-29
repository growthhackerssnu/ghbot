# gh-notion-bot 설치 (다른 노트북용)

## 포함된 것 / 빠진 것
- `server.py`, `notion_client.py`, `embed_index.py`, `requirements.txt` - 서버 코드
- `gh_bot.db` - 임베딩을 만들기 위한 로컬 staging DB. MCP 서버 실행 시에는
  읽지 않고 Supabase Postgres 테이블을 직접 조회합니다.
- `.env`는 **일부러 안 넣었습니다** - Notion Integration Secret이라 파일로 옮기지 않고
  아래에서 직접 붙여넣게 했습니다.

## 1. Python 준비
Python 3.12 이상 설치 후, 이 폴더에서:
```bash
pip install -r requirements.txt
```

## 2. 시크릿 설정
`.env.example`을 `.env`로 복사하고, Notion/Supabase 값을 실제 값으로 바꾸세요.
```bash
cp .env.example .env
```
Secret은 기존 노트북의 `.env` 파일에서 그대로 복사해오거나, Notion에서
[my-integrations](https://www.notion.so/my-integrations)에 들어가 같은 Integration(`ghbot 연결`)의
시크릿을 다시 확인하면 됩니다.

Supabase에서는 먼저 `supabase_schema.sql`을 SQL Editor에서 실행하세요.
그 다음 `SUPABASE_URL`과 private backend/service-role 성격의 `SUPABASE_KEY`를
설정합니다. 이 키는 서버 전용이므로 클라이언트나 저장소에 노출하면 안 됩니다.
기존 프로젝트의 `NEXT_PUBLIC_SUPABASE_URL`은 URL로도 인식되지만,
MCP 서버와 인덱서에는 반드시 `SUPABASE_KEY` 또는 `SUPABASE_SECRET_KEY`에 private/service-role 키를
넣으세요. `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`는 RLS가 적용된 인덱스에
사용할 수 없습니다.

## 3. 동작 확인
```bash
python server.py
```
에러 없이 떠 있으면 (Ctrl+C로 종료) 정상입니다.

## 4. 클라이언트에 등록

**Claude Code** - 이 폴더(또는 프로젝트 루트)에 `.mcp.json` 생성:
```json
{
  "mcpServers": {
    "gh-notion-bot": {
      "command": "<이 노트북의 python.exe 전체 경로>",
      "args": ["<이 폴더 전체 경로>\\server.py"]
    }
  }
}
```
`python.exe` 경로는 `where python`(cmd) 또는 `(Get-Command python).Source`(PowerShell)로 확인하세요.

**Codex** - `~/.codex/config.toml`에 추가:
```toml
[mcp_servers.gh_notion_bot]
command = '<이 노트북의 python.exe 전체 경로>'
args = ['<이 폴더 전체 경로>\server.py']
startup_timeout_sec = 30
```

등록 후 해당 앱을 완전히 재시작해야 반영됩니다.

## 5. 기존 인덱스 게시 또는 새로 만들기

이미 있는 `gh_bot.db`를 Supabase에 올리려면:

```bash
python3 embed_index.py --upload-only
```

노션 내용이 많이 바뀌었으면:
```bash
python3 embed_index.py
```
인덱싱이 끝나면 Supabase 테이블에도 자동 게시됩니다 (수 분 소요, 네트워크 필요).

운영 환경에서는 기존 MCP 웹 서비스와 별도로 Railway Cron 서비스를 하나
추가하세요. 이 저장소의 `railway.sync.json`을 설정 파일로 사용하거나 Railway
서비스 설정에 다음을 입력합니다:

```text
Start command: python sync_worker.py
Cron schedule: 0 18 * * *  # UTC 18:00 = 한국시간 03:00
```

Cron 서비스에도 Notion, Google Drive, Supabase 관련 환경변수를 동일하게
복사해야 합니다. 이 작업은 Supabase의 기존 인덱스를 staging DB에 복원한
뒤 변경된 프로젝트·Tasks 회의록만 다시 가져와서 Supabase에 게시하고 종료합니다.
People DB는 개인정보가 임베딩에 들어가지 않도록 동기화 대상에서 제외합니다.
