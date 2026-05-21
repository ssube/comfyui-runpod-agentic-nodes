from __future__ import annotations

try:
    from .config import get_runpod_api_key
    from .specs import DeploymentSpec, KeepAlivePolicy, NetworkStorageSpec, SSHCommandSpec
except ImportError:
    from config import get_runpod_api_key
    from specs import DeploymentSpec, KeepAlivePolicy, NetworkStorageSpec, SSHCommandSpec


class ValidationError(ValueError):
    pass


def validate_deployment(deployment: DeploymentSpec, *, mode: str = "plan", require_api_key: bool = True) -> list[str]:
    warnings: list[str] = []
    app = deployment.primary_app
    if app.llm_api and app.llm_server:
        raise ValidationError("Agent accepts either llm_api or llm_server, not both.")
    if app.browser and app.browser.engine == "neko" and app.browser.materialization == "same_pod":
        raise ValidationError("Neko browser supports only own_pod materialization in the MVP.")
    if app.llm_server and app.llm_server.materialization == "same_pod":
        raise ValidationError("LLM Server same_pod materialization is not supported in the MVP.")
    if app.sql_database and app.sql_database.engine == "sqlite":
        path = app.sql_database.runtime_contract.env.values.get("DATABASE_PATH", "")
        if not path:
            url = app.sql_database.runtime_contract.env.values.get("DATABASE_URL", "")
            path = url.removeprefix("sqlite:///")
        if path and not path.startswith(app.workspace_path):
            raise ValidationError("SQLite path must be inside the agent workspace path.")
        if deployment.network_storage is None:
            warnings.append("SQLite without network storage may be ephemeral.")
    for storage in network_storages(deployment):
        if not storage.network_volume_id and not (storage.size_gb and storage.data_center_id):
            raise ValidationError("Network storage requires network_volume_id, or create_size_gb with data_center_id.")
        if storage.retention_policy != "preserve":
            warnings.append(f"Network storage {storage.network_volume_id} uses retention_policy={storage.retention_policy}; verify this before destructive runs.")
    if deployment.s3_storage:
        if not deployment.s3_storage.access_key_secret.name or not deployment.s3_storage.secret_key_secret.name:
            raise ValidationError("S3 storage requires access and secret key secret references.")
    if deployment.keep_alive and deployment.keep_alive.mode == "cost":
        warnings.append("Cost keep-alive is estimated from pod runtime and cost/hour.")
    if deployment.ssh_commands:
        warnings.extend(validate_commands(deployment.ssh_commands))
    if mode in {"apply", "apply_and_wait", "stop", "terminate"} and require_api_key and not get_runpod_api_key():
        raise ValidationError(f"Run mode {mode} requires RUNPOD_API_KEY in the server environment.")
    return warnings


def network_storages(deployment: DeploymentSpec) -> list[NetworkStorageSpec]:
    app = deployment.primary_app
    storages = [deployment.network_storage]
    storages.extend(
        getattr(spec, "network_storage", None)
        for spec in (app.browser, app.llm_server, app.sql_database, app.vector_database)
    )
    return [storage for storage in storages if storage is not None]


def validate_commands(commands: SSHCommandSpec) -> list[str]:
    warnings: list[str] = []
    install_markers = ("apt install", "apt-get install", "pip install", "npm install", "pnpm install")
    for command in commands.commands:
        body = command.command.lower()
        if any(marker in body for marker in install_markers):
            warnings.append("Startup command appears to install dependencies; prefer baking common deps into templates.")
    return warnings


def validate_keep_alive(policy: KeepAlivePolicy) -> None:
    if policy.enforcement not in {"server_side", "pod_side", "both"}:
        raise ValidationError("Keep-alive enforcement must be server_side, pod_side, or both.")
    if policy.mode == "time" and not policy.time_seconds:
        raise ValidationError("Time keep-alive requires a positive time value.")
    if policy.mode == "turns" and not policy.turn_limit:
        raise ValidationError("Turns keep-alive requires a turn limit.")
    if policy.mode == "cost" and not policy.cost_limit_usd:
        raise ValidationError("Cost keep-alive requires a cost limit.")
