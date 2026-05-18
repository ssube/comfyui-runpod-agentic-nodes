from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .planner import DeploymentPlan, ResourcePlan
from .runner import startup_script_for_plan

DEFAULT_IMAGES = {
    "agent": "ubuntu:24.04",
    "browser:neko": "ghcr.io/m1k1o/neko/chromium:latest",
    "browser:playwright": "mcr.microsoft.com/playwright:v1.56.1-noble",
    "llm:ollama": "ollama/ollama:0.12.10",
    "llm:vllm": "vllm/vllm-openai:latest",
    "sql:postgres": "postgres:17.7",
    "sql:mysql": "mysql:8.4",
    "vector:qdrant": "qdrant/qdrant:v1.16.2",
    "vector:chroma": "chromadb/chroma:latest",
}

LOCAL_RUNTIME_ACTIONS = ("save_only", "config", "pull", "up", "down")


@dataclass(frozen=True)
class LocalApplyResult:
    engine: str
    action: str
    compose_path: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_text(self) -> str:
        return json.dumps(
            {
                "engine": self.engine,
                "action": self.action,
                "compose_path": self.compose_path,
                "command": self.command,
                "returncode": self.returncode,
                "stdout": self.stdout,
                "stderr": self.stderr,
            },
            indent=2,
            sort_keys=True,
        )


def compose_yaml_for_plan(plan: DeploymentPlan, *, project_name: str = "crag-local") -> str:
    service_names = {resource.name: compose_service_name(resource) for resource in plan.resources}
    services = {}
    volumes = {}
    for resource in plan.resources:
        service_name = service_names[resource.name]
        service = {
            "image": image_for_resource(resource),
            "container_name": f"{project_name}-{service_name}",
            "environment": compose_env(resource, service_names),
            "labels": {
                "comfyui-runpod-agentic.role": resource.role,
                "comfyui-runpod-agentic.desired_hash": resource.desired_hash,
            },
        }
        ports = compose_ports(resource)
        if ports:
            service["ports"] = ports
        command = compose_command(resource, plan)
        if command:
            service["command"] = escape_compose_interpolation(command)
        volume_mount = volume_mount_for_resource(resource)
        if volume_mount:
            volume_name, mount_path, retention_policy = volume_mount
            service["volumes"] = [f"{volume_name}:{mount_path}"]
            volumes[volume_name] = {"labels": {"comfyui-runpod-agentic.retention_policy": retention_policy}}
        services[service_name] = service
    compose = {
        "name": project_name,
        "services": services,
    }
    if volumes:
        compose["volumes"] = volumes
    return yaml.safe_dump(compose, sort_keys=False)


def compose_env(resource: ResourcePlan, service_names: dict[str, str]) -> dict[str, str]:
    env = dict(resource.pod_input.get("env") or {})
    for key, value in list(env.items()):
        env[key] = resolve_crag_placeholders(str(value), service_names)
    if resource.role == "sql" and env.get("DATABASE_KIND") == "postgres":
        env.setdefault("POSTGRES_DB", env.get("DATABASE_NAME", "app"))
        env.setdefault("POSTGRES_USER", env.get("DATABASE_USER", "app"))
        env.setdefault("POSTGRES_PASSWORD", env.get("DATABASE_PASSWORD", "app"))
    return env


def resolve_crag_placeholders(value: str, service_names: dict[str, str]) -> str:
    replacements = {
        "crag://browser/neko": ("browser", "http", 8080, ""),
        "crag://browser/playwright": ("browser", "http", 3000, ""),
        "crag://llm/ollama/v1": ("llm", "http", 11434, "/v1"),
        "crag://llm/ollama": ("llm", "http", 11434, ""),
        "crag://llm/vllm/v1": ("llm", "http", 8000, "/v1"),
        "crag://vector/qdrant": ("vector", "http", 6333, ""),
        "crag://vector/chroma": ("vector", "http", 8000, ""),
        "crag://sql/postgres": ("sql", "postgresql", 5432, ""),
        "crag://sql/mysql": ("sql", "mysql", 3306, ""),
    }
    for placeholder, (role, scheme, port, suffix) in replacements.items():
        if placeholder not in value:
            continue
        service = first_service_for_role(service_names, role)
        if not service:
            continue
        value = value.replace(placeholder, f"{scheme}://{service}:{port}{suffix}")
    return value


def first_service_for_role(service_names: dict[str, str], role: str) -> str | None:
    for name, service_name in service_names.items():
        if f"-{role}-" in name or name.startswith(f"crag-{role}-"):
            return service_name
    return None


def compose_ports(resource: ResourcePlan) -> list[str]:
    ports = []
    for port in resource.ports:
        container_port = int(port.get("container_port") or port.get("privatePort") or 0)
        if not container_port or container_port == 22:
            continue
        protocol = str(port.get("protocol") or port.get("type") or "tcp").lower()
        container_protocol = "tcp" if protocol == "http" else protocol
        ports.append(f"{container_port}:{container_port}/{container_protocol}")
    return ports


def compose_command(resource: ResourcePlan, plan: DeploymentPlan) -> str | None:
    if resource.role == "agent":
        return agent_startup_command(plan, resource)
    env = resource.pod_input.get("env") or {}
    if resource.role == "llm" and env.get("LLM_PROVIDER") == "ollama":
        return "serve"
    return None


def agent_startup_command(plan: DeploymentPlan, resource: ResourcePlan) -> str:
    commands = [
        action.detail
        for action in plan.actions
        if action.action == "RUN_SSH_COMMAND" and action.role == "agent" and action.resource_name == resource.name and action.detail.get("phase") in {"before_start", "after_start", "after_ready"}
    ]
    script = [
        "set -u",
        "mkdir -p /workspace/.runpod_agentic/local-runtime",
        "run_crag_command() {",
        "  label=\"$1\"",
        "  failure_policy=\"$2\"",
        "  retry_count=\"$3\"",
        "  body=\"$4\"",
        "  attempt=0",
        "  while true; do",
        "    echo \"[crag-local-runtime] running ${label} attempt ${attempt}\"",
        "    /bin/bash -lc \"$body\"",
        "    status=$?",
        "    if [ \"$status\" -eq 0 ]; then return 0; fi",
        "    if [ \"$failure_policy\" = \"continue\" ]; then",
        "      echo \"[crag-local-runtime] ${label} failed with ${status}; continuing\" >&2",
        "      return 0",
        "    fi",
        "    if [ \"$failure_policy\" = \"retry\" ] && [ \"$attempt\" -lt \"$retry_count\" ]; then",
        "      attempt=$((attempt + 1))",
        "      sleep 1",
        "      continue",
        "    fi",
        "    echo \"[crag-local-runtime] ${label} failed with ${status}\" >&2",
        "    return \"$status\"",
        "  done",
        "}",
    ]
    for index, command in enumerate(commands):
        label = command.get("source") or f"{command.get('phase', 'command')}:{index}"
        script.append(
            "run_crag_command "
            + " ".join(
                [
                    shlex.quote(str(label)),
                    shlex.quote(str(command.get("failure_policy") or "fail")),
                    shlex.quote(str(int(command.get("retry_count") or 0))),
                    shlex.quote(str(command.get("command") or "")),
                ]
            )
        )
    script.append("echo '[crag-local-runtime] startup commands complete'")
    script.append("sleep infinity")
    return "bash -lc " + shlex.quote("\n".join(script))


def image_for_resource(resource: ResourcePlan) -> str:
    env = resource.pod_input.get("env") or {}
    if resource.role == "agent":
        return DEFAULT_IMAGES["agent"]
    if resource.role == "browser":
        return DEFAULT_IMAGES.get(f"browser:{env.get('BROWSER_KIND')}", DEFAULT_IMAGES["browser:playwright"])
    if resource.role == "llm":
        return DEFAULT_IMAGES.get(f"llm:{env.get('LLM_PROVIDER')}", DEFAULT_IMAGES["llm:ollama"])
    if resource.role == "sql":
        return DEFAULT_IMAGES.get(f"sql:{env.get('DATABASE_KIND')}", DEFAULT_IMAGES["sql:postgres"])
    if resource.role == "vector":
        return DEFAULT_IMAGES.get(f"vector:{env.get('VECTOR_KIND')}", DEFAULT_IMAGES["vector:qdrant"])
    return "ubuntu:24.04"


def volume_mount_for_resource(resource: ResourcePlan) -> tuple[str, str, str] | None:
    volume_id = resource.pod_input.get("networkVolumeId")
    mount_path = resource.pod_input.get("volumeMountPath")
    if not volume_id or not mount_path:
        return None
    retention_policy = resource.storage_retention_policy or "preserve"
    return (compose_service_name_from_text(str(volume_id)), str(mount_path), retention_policy)


def compose_service_name(resource: ResourcePlan) -> str:
    return compose_service_name_from_text(resource.name)


def compose_service_name_from_text(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-") or "service"


def local_runtime_summary(plan: DeploymentPlan, compose_yaml: str) -> str:
    return json.dumps(
        {
            "run_id": plan.run_id,
            "services": [compose_service_name(resource) for resource in plan.resources],
            "startup_script_preview": startup_script_for_plan(plan).splitlines()[0],
            "compose_bytes": len(compose_yaml.encode("utf-8")),
        },
        indent=2,
        sort_keys=True,
    )


def write_compose_file(path: str | Path, content: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return str(target)


def apply_compose_file(
    engine: str,
    compose_path: str | Path,
    *,
    project_name: str = "crag-local",
    action: str = "config",
    timeout_seconds: int = 1800,
) -> LocalApplyResult:
    if action not in LOCAL_RUNTIME_ACTIONS:
        raise ValueError(f"Unsupported local runtime action: {action}")
    path = str(compose_path)
    if action == "save_only":
        return LocalApplyResult(engine, action, path, [], 0, "Compose file saved; no local runtime command was run.", "")
    try:
        command = command_for_engine(engine, path, project_name, action)
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, action, path, [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, action, path, exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, action, path, command, completed.returncode, completed.stdout, completed.stderr)


def command_for_engine(engine: str, compose_path: str, project_name: str, action: str) -> list[str]:
    if engine == "docker":
        return compose_command_for(local_runtime_base_command(["docker", "compose"]), compose_path, project_name, action)
    if engine == "podman":
        if shutil.which("podman"):
            return compose_command_for(local_runtime_base_command(["podman", "compose"]), compose_path, project_name, action)
        raise RuntimeError("Podman local runtime requires `podman compose` on PATH.")
    if engine == "containerd":
        if not shutil.which("nerdctl"):
            raise RuntimeError("Containerd local runtime requires `nerdctl` on PATH; raw ctr does not support compose semantics.")
        return compose_command_for(local_runtime_base_command(["nerdctl", "compose"]), compose_path, project_name, action)
    raise RuntimeError(f"Unsupported local runtime engine: {engine}")


def local_runtime_base_command(command: list[str]) -> list[str]:
    sudo = ["sudo"] if use_sudo_for_local_runtime() else []
    return [*sudo, *command]


def use_sudo_for_local_runtime() -> bool:
    value = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def compose_command_for(base: list[str], compose_path: str, project_name: str, action: str) -> list[str]:
    command = [*base, "-f", compose_path, "-p", project_name]
    if action == "up":
        return [*command, "up", "-d"]
    if action == "down":
        return [*command, "down", "--remove-orphans"]
    return [*command, action]


def escape_compose_interpolation(value: str) -> str:
    return value.replace("$", "$$")
