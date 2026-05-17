from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunpodAuthConfig:
    api_key_env: str = "RUNPOD_API_KEY"
    env_file_env: str = "RUNPOD_ENV_FILE"
    default_env_file: str = ".env.d/runpod.env"


@dataclass(frozen=True)
class SSHEnvConfig:
    proxy_suffix_env: str = "RUNPOD_SSH_PROXY_SUFFIX"
    private_key_env: str = "RUNPOD_SSH_PRIVATE_KEY_PATH"
    default_env_file: str = ".env.d/runpod.env"


def get_runpod_api_key(config: RunpodAuthConfig | None = None) -> str | None:
    auth = config or RunpodAuthConfig()
    value = os.environ.get(auth.api_key_env)
    if value:
        return value
    env_file = Path(os.environ.get(auth.env_file_env, auth.default_env_file))
    return read_env_file(env_file).get(auth.api_key_env)


def get_ssh_env_config(config: SSHEnvConfig | None = None) -> dict[str, str | None]:
    ssh = config or SSHEnvConfig()
    env_file = Path(os.environ.get("RUNPOD_ENV_FILE", ssh.default_env_file))
    file_values = read_env_file(env_file)
    return {
        "proxy_suffix": os.environ.get(ssh.proxy_suffix_env) or file_values.get(ssh.proxy_suffix_env),
        "private_key_path": os.environ.get(ssh.private_key_env) or file_values.get(ssh.private_key_env),
    }


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = unquote_env_value(value.strip())
    return values


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
