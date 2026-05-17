from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SSHError(RuntimeError):
    pass


class SSHClientProtocol(Protocol):
    def run(self, host: str, port: int, command: str, *, timeout_seconds: int | None = None) -> CommandResult: ...
    def write_file(self, host: str, port: int, path: str, content: str) -> None: ...


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class SSHConfig:
    username: str = "root"
    private_key_path: str = "~/.ssh/id_ed25519"
    connect_timeout_seconds: int = 120
    command_timeout_seconds: int = 1800


class SubprocessSSHClient:
    def __init__(self, config: SSHConfig | None = None):
        self.config = config or SSHConfig()

    def run(self, host: str, port: int, command: str, *, timeout_seconds: int | None = None) -> CommandResult:
        args = self._base_args(host, port) + [command]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds or self.config.command_timeout_seconds, check=False)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def write_file(self, host: str, port: int, path: str, content: str) -> None:
        mkdir = "mkdir -p " + shell_quote(str(Path(path).parent))
        result = self.run(host, port, mkdir)
        if result.exit_code != 0:
            raise SSHError(result.stderr)
        args = self._base_args(host, port) + [f"cat > {shell_quote(path)}"]
        proc = subprocess.run(args, input=content, capture_output=True, text=True, timeout=self.config.command_timeout_seconds, check=False)
        if proc.returncode != 0:
            raise SSHError(proc.stderr)

    def _base_args(self, host: str, port: int) -> list[str]:
        return [
            "ssh",
            "-i",
            str(Path(self.config.private_key_path).expanduser()),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_seconds}",
            "-p",
            str(port),
            f"{self.config.username}@{host}",
        ]


def extract_ssh_endpoint(pod: dict[str, Any]) -> tuple[str, int]:
    ports = ((pod.get("runtime") or {}).get("ports") or pod.get("ports") or [])
    for port in ports:
        private = port.get("privatePort") or port.get("containerPort") or port.get("container_port")
        public = port.get("publicPort") or port.get("public_port")
        ip = port.get("ip") or port.get("host") or port.get("hostname")
        port_type = str(port.get("type") or port.get("protocol") or "").lower()
        if int(private or 0) == 22 and public and ip and (not port_type or port_type == "tcp"):
            return str(ip), int(public)
    raise SSHError("Could not find public TCP mapping for pod SSH port 22.")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
