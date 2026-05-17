from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from .runtime_contracts import merge_contracts, secret_placeholder, with_env
    from .specs import (
        DeploymentSpec,
        RuntimeContract,
        SecretRef,
        SSHAccessPolicy,
        to_plain,
    )
    from .template_resolver import TemplateResolver
    from .validation import validate_deployment
except ImportError:
    from runtime_contracts import merge_contracts, secret_placeholder, with_env
    from specs import DeploymentSpec, RuntimeContract, SecretRef, SSHAccessPolicy, to_plain
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
    resources: list[ResourcePlan]
    runtime_contract: RuntimeContract
    ssh_access: SSHAccessPolicy
    actions: list[PlanAction]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


class Planner:
    def __init__(self, template_resolver: TemplateResolver | None = None):
        self.template_resolver = template_resolver or TemplateResolver()

    def build(self, deployment: DeploymentSpec, *, mode: str = "plan", prompt: Any = None) -> DeploymentPlan:
        warnings = validate_deployment(deployment, mode=mode, require_api_key=False)
        deployment_hash = stable_hash(to_plain(deployment))
        workflow_hash = stable_hash(prompt if prompt is not None else to_plain(deployment))[:12]
        run_id = f"crag-{uuid.uuid4().hex[:12]}"
        resources: list[ResourcePlan] = []
        dependency_contracts: list[RuntimeContract] = []

        for role, spec in self._own_pod_dependencies(deployment):
            selection = self.template_resolver.resolve_app(spec.kind, spec.engine)
            desired_hash = stable_hash(to_plain(spec))[:12]
            name = managed_name(workflow_hash, role, spec.meta.node_id, desired_hash)
            resources.append(
                ResourcePlan(
                    role=role,
                    node_id=spec.meta.node_id,
                    desired_hash=desired_hash,
                    name=name,
                    template_id=selection.template_id,
                    materialization=spec.materialization,
                    env=contract_env_for_creation(spec.runtime_contract),
                    secrets=spec.runtime_contract.env.secrets,
                    ports=[to_plain(port) for port in spec.runtime_contract.ports],
                    pod_input=self._pod_input(deployment, selection.template_id, name, role, spec.meta.node_id, desired_hash, spec.runtime_contract),
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

        agent_contract = merge_contracts(*local_contracts, *dependency_contracts)
        agent_contract = with_env(
            agent_contract,
            {
                "RUNPOD_MANAGED_BY": MANAGED_BY,
                "CRAG_RUN_ID": run_id,
                "CRAG_WORKFLOW_HASH": workflow_hash,
                "CRAG_NODE_ID": deployment.primary_app.meta.node_id or "",
                "CRAG_ROLE": "agent",
                "WORKSPACE_DIR": deployment.primary_app.workspace_path,
            },
        )

        agent_selection = self.template_resolver.resolve_agent(
            deployment.primary_app.harness,
            deployment.primary_app.required_image_capabilities,
        )
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
                ports=[to_plain(port) for port in agent_contract.ports],
                pod_input=self._pod_input(deployment, agent_selection.template_id, agent_name, "agent", deployment.primary_app.meta.node_id, agent_hash, agent_contract),
            )
        )

        actions = self._actions(resources, deployment)
        return DeploymentPlan(run_id, workflow_hash, deployment_hash, mode, resources, agent_contract, deployment.ssh_access, actions, warnings)

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
            "ports": [to_plain(port) for port in contract.ports],
            "startSsh": True,
            "containerDiskInGb": hints.container_disk_gb,
            "gpuCount": hints.gpu_count,
            "cloudType": hints.cloud_type,
        }
        if hints.gpu_type_id:
            pod_input["gpuTypeId"] = hints.gpu_type_id
        if hints.volume_gb:
            pod_input["volumeInGb"] = hints.volume_gb
        if deployment.network_storage:
            pod_input["networkVolumeId"] = deployment.network_storage.network_volume_id
            pod_input["volumeMountPath"] = deployment.network_storage.mount_path
        if deployment.keep_alive and deployment.keep_alive.mode == "time" and deployment.keep_alive.time_seconds:
            field_name = "stopAfter" if deployment.keep_alive.action == "stop" else "terminateAfter"
            pod_input[field_name] = (datetime.now(UTC) + timedelta(seconds=deployment.keep_alive.time_seconds)).isoformat()
        return pod_input

    def _actions(self, resources: list[ResourcePlan], deployment: DeploymentSpec) -> list[PlanAction]:
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
        for command in sorted((deployment.ssh_commands.commands if deployment.ssh_commands else []), key=lambda item: item.order):
            if command.phase == "before_start":
                actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, to_plain(command)))
        actions.append(PlanAction("WRITE_RUNTIME_CONFIG", "agent", agent.name, {"files": runtime_config_paths(deployment.primary_app.workspace_path)}))
        actions.append(PlanAction("LAUNCH_AGENT", "agent", agent.name, {"harness": deployment.primary_app.harness, "startup_mode": deployment.primary_app.startup_mode}))
        for command in sorted((deployment.ssh_commands.commands if deployment.ssh_commands else []), key=lambda item: item.order):
            if command.phase in {"after_start", "after_ready"}:
                actions.append(PlanAction("RUN_SSH_COMMAND", "agent", agent.name, to_plain(command)))
        if deployment.keep_alive:
            actions.append(PlanAction("MONITOR_KEEP_ALIVE", "agent", agent.name, to_plain(deployment.keep_alive)))
        return actions


def contract_env_for_creation(contract: RuntimeContract) -> dict[str, str]:
    env = dict(contract.env.values)
    for secret in contract.env.secrets:
        env[secret.env_var] = secret_placeholder(secret)
    return env


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
