# gh-notion-bot 설치 (다른 노트북용)

## 포함된 것 / 빠진 것
- `server.py`, `notion_client.py`, `embed_index.py`, `requirements.txt` - 서버 코드
- `gh_bot.db` - 이미 만들어진 임베딩 인덱스 (135페이지·2,497청크). 그대로 쓰면 되고,
  새로 인덱싱하고 싶을 때만 `embed_index.py`를 다시 돌리면 됩니다.
- `.env`는 **일부러 안 넣었습니다** - Notion Integration Secret이라 파일로 옮기지 않고
  아래에서 직접 붙여넣게 했습니다.

## 1. Python 준비
Python 3.12 이상 설치 후, 이 폴더에서:
```bash
pip install -r requirements.txt
```

## 2. 시크릿 설정
`.env.example`을 `.env`로 복사하고, 안의 값을 실제 Notion Integration Secret으로 바꾸세요.
```bash
cp .env.example .env
```
Secret은 기존 노트북의 `.env` 파일에서 그대로 복사해오거나, Notion에서
[my-integrations](https://www.notion.so/my-integrations)에 들어가 같은 Integration(`ghbot 연결`)의
시크릿을 다시 확인하면 됩니다.

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

## 5. (선택) 인덱스 새로 만들기
노션 내용이 많이 바뀌었으면:
```bash
python embed_index.py
```
`gh_bot.db`를 통째로 재생성합니다 (수 분 소요, 네트워크 필요).
