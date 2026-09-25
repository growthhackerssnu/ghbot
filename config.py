"""Small configuration helpers for local dotenv files and JSON secrets."""

import json
import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load simple KEY=value entries without choking on multiline JSON.

    JSON secrets are intentionally skipped here and read lazily by
    ``load_json_env``. This keeps python-dotenv-style loading compatible with
    both one-line deployment secrets and the multiline local .env format.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or value.startswith(("{", "[")):
            continue
        if key not in os.environ:
            os.environ[key] = value.strip("\"'")


def load_json_env(name: str, env_file: Path | None = None):
    """Read a JSON environment value, falling back to raw multiline .env."""
    value = os.environ.get(name, "").strip()
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass

    path = env_file or Path(__file__).parent / ".env"
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{name}="):
            continue
        candidate = line.split("=", 1)[1].strip()
        if not candidate.startswith(("{", "[")):
            continue
        parts = [candidate]
        for continuation in lines[index + 1 :]:
            if not continuation.strip():
                break
            parts.append(continuation)
            try:
                return json.loads("\n".join(parts))
            except json.JSONDecodeError:
                continue
        try:
            return json.loads("\n".join(parts))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} is not valid JSON in {path}") from exc
    return None
