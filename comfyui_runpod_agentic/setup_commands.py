from __future__ import annotations

import shlex

from .specs import SkillSource


def skill_install_command(skill: SkillSource) -> str:
    lines = [
        "set -e",
        "if ! command -v git >/dev/null 2>&1; then",
        "  if command -v apt-get >/dev/null 2>&1; then",
        "    apt-get update",
        "    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates",
        "  elif command -v apk >/dev/null 2>&1; then",
        "    apk add --no-cache git ca-certificates",
        "  else",
        "    echo 'No supported package manager found for installing git' >&2",
        "    exit 1",
        "  fi",
        "fi",
        "tmp=$(mktemp -d)",
        "trap 'rm -rf \"$tmp\"' EXIT",
        f"git clone --depth 1 {shlex.quote(skill.repo_url)} \"$tmp/repo\"",
    ]
    if skill.git_ref:
        lines.extend(
            [
                f"git -C \"$tmp/repo\" fetch --depth 1 origin {shlex.quote(skill.git_ref)}",
                "git -C \"$tmp/repo\" checkout FETCH_HEAD",
            ]
        )
    lines.extend(
        [
            f"src=\"$tmp/repo/{shell_path_fragment(skill.repo_path)}\"",
            f"target={shlex.quote(skill.target_path)}",
            "test -d \"$src\"",
        ]
    )
    if skill.kind == "framework":
        lines.extend(
            [
                "mkdir -p \"$target\"",
                "cp -a \"$src\"/. \"$target\"/",
            ]
        )
    else:
        lines.extend(
            [
                "rm -rf \"$target\"",
                "mkdir -p \"$target\"",
                "cp -a \"$src\"/. \"$target\"/",
            ]
        )
    return "\n".join(lines)


def local_sql_setup_command(database_path: str, database_name: str) -> str:
    path = database_path or f"/workspace/db/{database_name}.sqlite"
    return "\n".join(
        [
            "set -e",
            "if ! command -v sqlite3 >/dev/null 2>&1; then",
            "  if command -v apt-get >/dev/null 2>&1; then",
            "    apt-get update",
            "    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends sqlite3 libsqlite3-0",
            "  elif command -v apk >/dev/null 2>&1; then",
            "    apk add --no-cache sqlite-libs sqlite",
            "  elif command -v dnf >/dev/null 2>&1; then",
            "    dnf install -y sqlite",
            "  else",
            "    echo 'sqlite3 is required for local SQL but no supported package manager was found' >&2",
            "    exit 1",
            "  fi",
            "fi",
            f"mkdir -p \"$(dirname {shlex.quote(path)})\"",
            f"touch {shlex.quote(path)}",
            f"sqlite3 {shlex.quote(path)} 'PRAGMA user_version;'",
        ]
    )


def shell_path_fragment(path: str) -> str:
    return path.strip().lstrip("/") or "."
