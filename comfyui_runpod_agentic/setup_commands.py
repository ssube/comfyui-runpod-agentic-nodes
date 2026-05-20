from __future__ import annotations

import shlex

from .specs import SkillSource

HARNESS_INSTALLS = {
    "codex": {
        "binary": "codex",
        "install": "npm install -g @openai/codex",
        "requires_node": True,
    },
    "claude": {
        "binary": "claude",
        "install": "npm install -g @anthropic-ai/claude-code",
        "requires_node": True,
    },
    "opencode": {
        "binary": "opencode",
        "install": "npm install -g opencode-ai",
        "requires_node": True,
    },
    "hermes": {
        "binary": "hermes",
        "install": "pipx install hermes-agent",
        "requires_pipx": True,
    },
}


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


def harness_install_command(harness: str) -> str:
    harness_id = harness.strip().lower().replace(" ", "_")
    install = HARNESS_INSTALLS.get(harness_id)
    if not install:
        raise ValueError(f"Unsupported harness: {harness}")
    binary = install["binary"]
    lines = ["set -e", "export PATH=\"$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:$PATH\""]
    if install.get("requires_node"):
        lines.extend(nodejs_runtime_install_lines())
    if install.get("requires_pipx"):
        lines.extend(python_runtime_install_lines())
        lines.extend(
            [
                "if ! command -v pipx >/dev/null 2>&1; then",
                "  python3 -m pip install --user pipx",
                "  python3 -m pipx ensurepath || true",
                "fi",
            ]
        )
    lines.extend(
        [
            f"if ! command -v {shlex.quote(binary)} >/dev/null 2>&1; then",
            f"  {install['install']}",
            "fi",
            f"{shlex.quote(binary)} --help >/dev/null",
        ]
    )
    return "\n".join(lines)


def package_install_command(package_manager: str, packages: str) -> str:
    manager = package_manager.strip().lower()
    package_args = shlex.split(packages or "")
    if not package_args:
        raise ValueError("At least one package is required.")
    quoted = " ".join(shlex.quote(package) for package in package_args)
    if manager == "apt":
        return "\n".join(
            [
                "set -e",
                "if ! command -v apt-get >/dev/null 2>&1; then echo 'apt-get is required' >&2; exit 1; fi",
                "apt-get update",
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {quoted}",
            ]
        )
    if manager == "npm":
        return "\n".join([*nodejs_runtime_install_lines(), f"npm install -g {quoted}"])
    if manager == "pip":
        return "\n".join([*python_runtime_install_lines(), f"python3 -m pip install {quoted}"])
    raise ValueError(f"Unsupported package manager: {package_manager}")


def language_runtime_install_command(runtime: str, node_major_version: int = 22) -> str:
    runtime_id = runtime.strip().lower()
    if runtime_id == "nodejs":
        return "\n".join(nodejs_runtime_install_lines(node_major_version))
    if runtime_id == "python":
        return "\n".join(python_runtime_install_lines())
    raise ValueError(f"Unsupported language runtime: {runtime}")


def nodejs_runtime_install_lines(node_major_version: int = 22) -> list[str]:
    version = int(node_major_version or 22)
    return [
        "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then",
        "  if ! command -v apt-get >/dev/null 2>&1; then echo 'apt-get is required to install Node.js' >&2; exit 1; fi",
        "  apt-get update",
        "  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl gnupg",
        "  mkdir -p /etc/apt/keyrings",
        "  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg",
        f"  echo 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_{version}.x nodistro main' > /etc/apt/sources.list.d/nodesource.list",
        "  apt-get update",
        "  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs",
        "fi",
    ]


def python_runtime_install_lines() -> list[str]:
    return [
        "if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then",
        "  if ! command -v apt-get >/dev/null 2>&1; then echo 'apt-get is required to install Python' >&2; exit 1; fi",
        "  apt-get update",
        "  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-pip python3-venv pipx",
        "fi",
    ]


def container_snapshot_command(tag: str, runtime: str, push: bool, username_env: str, password_env: str) -> str:
    image_tag = tag.strip()
    if not image_tag:
        raise ValueError("Snapshot tag is required.")
    runtime_id = runtime.strip().lower()
    if runtime_id not in {"docker", "podman", "nerdctl"}:
        raise ValueError(f"Unsupported snapshot runtime: {runtime}")
    lines = [
        "set -e",
        f"runtime={shlex.quote(runtime_id)}",
        f"image_tag={shlex.quote(image_tag)}",
        "if ! command -v \"$runtime\" >/dev/null 2>&1; then echo \"$runtime is required for container snapshots\" >&2; exit 1; fi",
        "container_id=\"${HOST_CONTAINER_ID:-${RUNPOD_POD_ID:-$(hostname)}}\"",
        "\"$runtime\" commit \"$container_id\" \"$image_tag\"",
    ]
    if push:
        user = username_env.strip() or "DOCKERHUB_USERNAME"
        password = password_env.strip() or "DOCKERHUB_TOKEN"
        lines.extend(
            [
                f"if [ -n \"${{{user}:-}}\" ] && [ -n \"${{{password}:-}}\" ]; then",
                f"  printf '%s' \"${{{password}}}\" | \"$runtime\" login docker.io -u \"${{{user}}}\" --password-stdin",
                "fi",
                "\"$runtime\" push \"$image_tag\"",
            ]
        )
    return "\n".join(lines)


def shell_path_fragment(path: str) -> str:
    return path.strip().lstrip("/") or "."
