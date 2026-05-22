from __future__ import annotations

import base64
import hashlib
import json
import shlex
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .runtime_contracts import merge_contracts, secret_placeholder, with_env
    from .specs import (
        DeploymentSpec,
        KeepAlivePolicy,
        NetworkStorageSpec,
        RuntimeContract,
        SecretRef,
        SSHAccessPolicy,
        to_plain,
    )
    from .template_resolver import TemplateResolver
    from .validation import validate_deployment
except ImportError:
    from runtime_contracts import merge_contracts, secret_placeholder, with_env
    from specs import DeploymentSpec, KeepAlivePolicy, NetworkStorageSpec, RuntimeContract, SecretRef, SSHAccessPolicy, to_plain
    from template_resolver import TemplateResolver
    from validation import validate_deployment

MANAGED_BY = "comfyui-runpod-agentic"
NAME_PREFIX = "crag"


@dataclass(frozen=True)
class ResourcePlan:
    role: str
    node_id: str | None
    desired_hash: str
    name: str
    template_id: str | None
    materialization: str
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[SecretRef] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    pod_input: dict[str, Any] = field(default_factory=dict)
    storage_retention_policy: str | None = None


@dataclass(frozen=True)
class PlanAction:
    action: str
    role: str | None = None
    resource_name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentPlan:
    run_id: str
    workflow_hash: str
    deployment_hash: str
    mode: str
    prompt: str
    resources: list[ResourcePlan]
    runtime_contract: RuntimeContract
    ssh_access: SSHAccessPolicy
    reuse_policy: str
    keep_alive: KeepAlivePolicy | None
    actions: list[PlanAction]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


class Planner:
    def __init__(self, template_resolver: TemplateResolver | None = None):
        self.template_resolver = template_resolver or TemplateResolver()

    def build(self, deployment: DeploymentSpec, *, mode: str = "plan", prompt: str = "", workflow_graph: Any = None) -> DeploymentPlan:
        warnings = validate_deployment(deployment, mode=mode, require_api_key=False)
        deployment_hash = stable_hash(to_plain(deployment))
        workflow_hash = stable_hash(workflow_graph if workflow_graph is not None else to_plain(deployment))[:12]
        run_id = f"crag-{uuid.uuid4().hex[:12]}"
        resources: list[ResourcePlan] = []
        dependency_contracts: list[RuntimeContract] = []

        for role, spec in self._own_pod_dependencies(deployment):
            selection = self.template_resolver.resolve_app(spec.kind, spec.engine)
            pod_contract = dependency_pod_contract(role, spec.runtime_contract)
            desired_hash = stable_hash(to_plain(spec))[:12]
            name = managed_name(workflow_hash, role, spec.meta.node_id, desired_hash)
            storage = getattr(spec, "network_storage", None)
            resources.append(
                ResourcePlan(
                    role=role,
                    node_id=spec.meta.node_id,
                    desired_hash=desired_hash,
                    name=name,
                    template_id=selection.template_id,
                    materialization=spec.materialization,
                    env=contract_env_for_creation(pod_contract),
                    secrets=pod_contract.env.secrets,
                    ports=[to_plain(port) for port in pod_contract.ports],
                    pod_input=self._pod_input(deployment, selection.template_id, name, role, spec.meta.node_id, desired_hash, pod_contract, network_storage=storage),
                    storage_retention_policy=storage.retention_policy if storage else None,
                )
            )
            dependency_contracts.append(spec.runtime_contract)

        local_contracts = [deployment.primary_app.runtime_contract]
        for spec in (deployment.primary_app.browser, deployment.primary_app.sql_database, deployment.primary_app.vector_database):
            if spec and spec.materialization in {"same_pod", "file_only", "env_only", "config_only"}:
                local_contracts.append(spec.runtime_contract)
        if deployment.primary_app.llm_api:
            local_contracts.append(deployment.primary_app.llm_api.runtime_contract)
        if deployment.s3_storage:
            local_contracts.append(deployment.s3_storage.runtime_contract)

        local_contract = merge_contracts(*local_contracts)
        agent_contract = merge_contracts(local_contract, *dependency_contracts)
        agent_contract = with_env(
            agent_contract,
            {
                "RUNPOD_MANAGED_BY": MANAGED_BY,
                "CRAG_RUN_ID": run_id,
                "CRAG_WORKFLOW_HASH": workflow_hash,
                "CRAG_NODE_ID": deployment.primary_app.meta.node_id or "",
                "CRAG_ROLE": "agent",
                "AGENT_PROMPT": prompt,
                "WORKSPACE_DIR": deployment.primary_app.workspace_path,
            },
        )
        agent_contract = with_env(agent_contract, keep_alive_env(deployment.keep_alive))

        agent_selection = self.template_resolver.resolve_agent(
            deployment.primary_app.harness,
            deployment.primary_app.required_image_capabilities,
        )
        agent_pod_contract = replace(agent_contract, ports=local_contract.ports)
        agent_hash = stable_hash(to_plain(deployment.primary_app))[:12]
        agent_name = managed_name(workflow_hash, "agent", deployment.primary_app.meta.node_id, agent_hash)
        resources.append(
            ResourcePlan(
                role="agent",
                node_id=deployment.primary_app.meta.node_id,
                desired_hash=agent_hash,
                name=agent_name,
                template_id=agent_selection.template_id,
                materialization="own_pod",
                env=contract_env_for_creation(agent_contract),
                secrets=agent_contract.env.secrets,
                ports=[to_plain(port) for port in agent_pod_contract.ports],
                pod_input=self._pod_input(deployment, agent_selection.template_id, agent_name, "agent", deployment.primary_app.meta.node_id, agent_hash, agent_pod_contract, network_storage=deployment.network_storage, install_sshd=deployment.ssh_access.install_internal_sshd),
                storage_retention_policy=deployment.network_storage.retention_policy if deployment.network_storage else None,
            )
        )

        actions = self._actions(resources, deployment, agent_contract)
        return DeploymentPlan(run_id, workflow_hash, deployment_hash, mode, prompt, resources, agent_contract, deployment.ssh_access, deployment.reuse_policy, deployment.keep_alive, actions, warnings)

    def _own_pod_dependencies(self, deployment: DeploymentSpec) -> list[tuple[str, Any]]:
        app = deployment.primary_app
        deps = [
            ("llm", app.llm_server),
            ("sql", app.sql_database),
            ("vector", app.vector_database),
            ("browser", app.browser),
        ]
        return [(role, spec) for role, spec in deps if spec is not None and spec.materialization == "own_pod"]

    def _pod_input(
        self,
        deployment: DeploymentSpec,
        template_id: str,
        name: str,
        role: str,
        node_id: str | None,
        desired_hash: str,
        contract: RuntimeContract,
        *,
        network_storage: NetworkStorageSpec | None = None,
        install_sshd: bool = False,
    ) -> dict[str, Any]:
        hints = deployment.resource_hints
        env = contract_env_for_creation(contract)
        env.update(
            {
                "RUNPOD_MANAGED_BY": MANAGED_BY,
                "CRAG_NODE_ID": node_id or "",
                "CRAG_ROLE": role,
                "CRAG_DESIRED_HASH": desired_hash,
            }
        )
        pod_input: dict[str, Any] = {
            "templateId": template_id,
            "name": name,
            "env": env,
            "ports": ensure_ssh_port([to_plain(port) for port in contract.ports]),
            "startSsh": True,
            "containerDiskInGb": hints.container_disk_gb,
            "gpuCount": hints.gpu_count,
            "cloudType": hints.cloud_type,
        }
        if hints.gpu_type_id:
            pod_input["gpuTypeId"] = hints.gpu_type_id
        if hints.volume_gb:
            pod_input["volumeInGb"] = hints.volume_gb
        if network_storage:
            if network_storage.network_volume_id:
                pod_input["networkVolumeId"] = network_storage.network_volume_id
            else:
                pod_input["_networkVolumeName"] = network_storage.name or name
                pod_input["_networkVolumeSizeGb"] = network_storage.size_gb
                pod_input["_networkVolumeDataCenterId"] = network_storage.data_center_id
            pod_input["volumeMountPath"] = network_storage.mount_path
        if deployment.keep_alive and deployment.keep_alive.mode == "time" and deployment.keep_alive.time_seconds and deployment.keep_alive.enforcement in {"server_side", "both"}:
            field_name = "stopAfter" if deployment.keep_alive.action == "stop" else "terminateAfter"
            pod_input[field_name] = (datetime.now(UTC) + timedelta(seconds=deployment.keep_alive.time_seconds)).isoformat()
        if install_sshd:
            public_key = read_public_key(deployment.ssh_access.private_key_path)
            if public_key:
                env["RUNPOD_SSH_PUBLIC_KEY"] = public_key
            pod_input["dockerArgs"] = internal_sshd_command()
        return pod_input

    def _actions(self, resources: list[ResourcePlan], deployment: DeploymentSpec, agent_contract: RuntimeContract) -> list[PlanAction]:
        deps = [resource for resource in resources if resource.role != "agent"]
        agent = next(resource for resource in resources if resource.role == "agent")
        actions: list[PlanAction] = []
        for resource in deps:
            actions.append(PlanAction("CREATE_OR_RESUME", resource.role, resource.name, {"template_id": resource.template_id}))
        for resource in deps:
            actions.append(PlanAction("WAIT_READY", resource.role, resource.name))
        if deps:
            actions.append(PlanAction("RESOLVE_DEPENDENCY_CONTRACTS", detail={"resources": [resource.name for resource in deps]}))
        actions.append(PlanAction("CREATE_OR_RESUME", "agent", agent.name, {"template_id": agent.template_id}))
        actions.append(PlanAction("WAIT_SSH", "agent", agent.name))
        actions.extend(runtime_command_actions(agent, agent_contract, {"before_start"}))
        for command in sorted((deployment.ssh_commands.commands if deployment.ssh_commands else []), key=lambda item: item.order):
            if command.phase == "before_start":
                actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, to_plain(command)))
        actions.append(PlanAction("WRITE_RUNTIME_CONFIG", "agent", agent.name, {"files": runtime_config_paths(deployment.primary_app.workspace_path)}))
        actions.append(PlanAction("LAUNCH_AGENT", "agent", agent.name, {"harness": deployment.primary_app.harness, "startup_mode": deployment.primary_app.startup_mode}))
        actions.extend(runtime_command_actions(agent, agent_contract, {"after_start", "after_ready"}))
        for command in sorted((deployment.ssh_commands.commands if deployment.ssh_commands else []), key=lambda item: item.order):
            if command.phase in {"after_start", "after_ready"}:
                actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, to_plain(command)))
        if deployment.keep_alive:
            actions.append(PlanAction("MONITOR_KEEP_ALIVE", "agent", agent.name, to_plain(deployment.keep_alive)))
        for command in sorted((deployment.ssh_commands.commands if deployment.ssh_commands else []), key=lambda item: item.order):
            if command.phase == "teardown":
                actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, to_plain(command)))
        return actions


def contract_env_for_creation(contract: RuntimeContract) -> dict[str, str]:
    env = dict(contract.env.values)
    for secret in contract.env.secrets:
        env[secret.env_var] = secret_placeholder(secret)
    return env


def keep_alive_env(policy: KeepAlivePolicy | None) -> dict[str, str]:
    if not policy or policy.mode == "manual":
        return {}
    values = {
        "CRAG_KEEP_ALIVE_MODE": policy.mode,
        "CRAG_KEEP_ALIVE_ACTION": policy.action,
        "CRAG_KEEP_ALIVE_ENFORCEMENT": policy.enforcement,
    }
    if policy.turn_limit is not None:
        values["CRAG_KEEP_ALIVE_TURN_LIMIT"] = str(policy.turn_limit)
    if policy.cost_limit_usd is not None:
        values["CRAG_KEEP_ALIVE_COST_LIMIT_USD"] = str(policy.cost_limit_usd)
    if policy.idle_grace_seconds is not None:
        values["CRAG_KEEP_ALIVE_IDLE_GRACE_SECONDS"] = str(policy.idle_grace_seconds)
    return values


def dependency_pod_contract(role: str, contract: RuntimeContract) -> RuntimeContract:
    env = dict(contract.env.values)
    if role == "llm":
        if env.get("LLM_PROVIDER") == "ollama":
            env["OLLAMA_HOST"] = "0.0.0.0:11434"
            env.pop("OPENAI_BASE_URL", None)
        elif env.get("LLM_PROVIDER") == "vllm":
            env.pop("OPENAI_BASE_URL", None)
    elif role == "browser":
        env.pop("NEKO_URL", None)
        env.pop("PLAYWRIGHT_WS_ENDPOINT", None)
    elif role == "sql":
        env.pop("DATABASE_URL", None)
    elif role == "vector":
        env.pop("VECTOR_URL", None)
    return replace(contract, env=replace(contract.env, values=env))


def runtime_command_actions(agent: ResourcePlan, contract: RuntimeContract, phases: set[str]) -> list[PlanAction]:
    actions = []
    commands = sorted((command for command in contract.commands if command.phase in phases), key=lambda item: item.order)
    for command in commands:
        detail = to_plain(command)
        detail["command_hash"] = stable_hash(detail)[:12]
        actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, detail))
    return actions


def ensure_ssh_port(ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for port in ports:
        if int(port.get("container_port") or port.get("privatePort") or 0) == 22:
            return ports
    return [*ports, {"name": "ssh", "container_port": 22, "protocol": "tcp", "public": True}]


def read_public_key(private_key_path: str) -> str | None:
    pub_path = Path(private_key_path).expanduser().with_suffix(Path(private_key_path).suffix + ".pub")
    if pub_path.exists():
        return pub_path.read_text().strip()
    return None


def internal_sshd_command() -> str:
    script = r"""
set -e
if ! command -v sshd >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache openssh-server
  else
    echo "No supported package manager found for installing openssh-server" >&2
    exit 1
  fi
fi
mkdir -p /run/sshd /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
if [ -n "${RUNPOD_SSH_PUBLIC_KEY:-}" ] && ! grep -qxF "$RUNPOD_SSH_PUBLIC_KEY" /root/.ssh/authorized_keys; then
  printf '%s\n' "$RUNPOD_SSH_PUBLIC_KEY" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A
if [ -f /etc/ssh/sshd_config ]; then
  sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
fi
/usr/sbin/sshd
sleep infinity
""".strip()
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = f"echo {encoded} | base64 -d > /tmp/runpod-agentic-sshd.sh && bash /tmp/runpod-agentic-sshd.sh"
    return "bash -lc " + shlex.quote(command)


def runtime_config_paths(workspace_path: str) -> dict[str, str]:
    base = workspace_path.rstrip("/") + "/.runpod_agentic"
    return {
        "resources": f"{base}/resources.json",
        "session_env": f"{base}/session.env",
        "commands": f"{base}/commands.json",
    }


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def short_node_id(node_id: str | None) -> str:
    if not node_id:
        return "node"
    return "".join(ch for ch in str(node_id).lower() if ch.isalnum())[:10] or "node"


def managed_name(workflow_hash: str, role: str, node_id: str | None, desired_hash: str) -> str:
    return f"{NAME_PREFIX}-{workflow_hash[:12]}-{role}-{short_node_id(node_id)}-{desired_hash[:12]}"
