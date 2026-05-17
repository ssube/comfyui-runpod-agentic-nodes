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
        args = self._base_args(host, port, allocate_tty=is_runpod_proxy_host(host)) + [command]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds or self.config.command_timeout_seconds, check=False)
        return normalize_ssh_result(CommandResult(proc.returncode, proc.stdout, proc.stderr))

    def write_file(self, host: str, port: int, path: str, content: str) -> None:
        mkdir = "mkdir -p " + shell_quote(str(Path(path).parent))
        result = self.run(host, port, mkdir)
        if result.exit_code != 0:
            raise SSHError(result.stderr)
        args = self._base_args(host, port, allocate_tty=is_runpod_proxy_host(host)) + [f"cat > {shell_quote(path)}"]
        proc = subprocess.run(args, input=content, capture_output=True, text=True, timeout=self.config.command_timeout_seconds, check=False)
        result = normalize_ssh_result(CommandResult(proc.returncode, proc.stdout, proc.stderr))
        if result.exit_code != 0:
            raise SSHError(result.stderr or result.stdout)

    def _base_args(self, host: str, port: int, *, allocate_tty: bool = False) -> list[str]:
        target = host if "@" in host else f"{self.config.username}@{host}"
        args = [
            "ssh",
            "-i",
            str(Path(self.config.private_key_path).expanduser()),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_seconds}",
            "-p",
            str(port),
            target,
        ]
        if allocate_tty:
            args.insert(1, "-tt")
        return args


def runpod_proxy_ssh_endpoint(pod: dict[str, Any], proxy_key_suffix: str | None = None) -> tuple[str, int]:
    pod_id = pod.get("id")
    if not pod_id:
        raise SSHError("Runpod proxy SSH requires a pod id.")
    if not proxy_key_suffix:
        raise SSHError("Runpod proxy SSH requires proxy_key_suffix, for example RUNPOD_SSH_PROXY_SUFFIX=64410ecc.")
    return f"{pod_id}-{proxy_key_suffix}@ssh.runpod.io", 22


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


def is_runpod_proxy_host(host: str) -> bool:
    return "ssh.runpod.io" in host


def normalize_ssh_result(result: CommandResult) -> CommandResult:
    combined = result.stdout + result.stderr
    if "Your SSH client doesn't support PTY" in combined:
        return CommandResult(255, result.stdout, result.stderr or "Runpod proxy SSH requires PTY allocation.")
    return result
