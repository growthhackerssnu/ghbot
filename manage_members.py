"""Manage the member allowlist for the remote (HTTP) gh-notion-bot deployment.

Each member gets their own token (so access can be revoked individually
without breaking everyone else) stored in members.json as {token: name}.
Never commit members.json - it's in .gitignore.

Usage:
    python manage_members.py add "양기서"
    python manage_members.py list
    python manage_members.py revoke "양기서"
"""
import json
import secrets
import sys
from pathlib import Path

MEMBERS_FILE = Path(__file__).parent / "members.json"


def load() -> dict[str, str]:
    if not MEMBERS_FILE.exists():
        return {}
    return json.loads(MEMBERS_FILE.read_text(encoding="utf-8"))


def save(members: dict[str, str]) -> None:
    MEMBERS_FILE.write_text(json.dumps(members, ensure_ascii=False, indent=2), encoding="utf-8")


def add(name: str) -> None:
    members = load()
    if name in members.values():
        print(f"'{name}' already has a token - use revoke first if you want to reissue one.")
        return
    token = "ghbot_" + secrets.token_urlsafe(24)
    members[token] = name
    save(members)
    print(f"added '{name}'. token (share this with them, once):\n{token}")


def list_members() -> None:
    members = load()
    if not members:
        print("no members yet")
        return
    for token, name in members.items():
        print(f"{name}: {token[:12]}...{token[-4:]}")


def revoke(name: str) -> None:
    members = load()
    remaining = {t: n for t, n in members.items() if n != name}
    if len(remaining) == len(members):
        print(f"no token found for '{name}'")
        return
    save(remaining)
    print(f"revoked '{name}'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd, *args = sys.argv[1:]
    if cmd == "add" and args:
        add(args[0])
    elif cmd == "list":
        list_members()
    elif cmd == "revoke" and args:
        revoke(args[0])
    else:
        print(__doc__)
