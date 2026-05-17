from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .planner import Planner
from .runner import default_state_path
from .specs import (
    AgentSpec,
    BrowserSpec,
    DeploymentSpec,
    EnvPatch,
    KeepAlivePolicy,
    LLMApiSpec,
    LLMServerSpec,
    NetworkStorageSpec,
    PodResourceHints,
    PortSpec,
    RuntimeContract,
    S3StorageSpec,
    SecretRef,
    SpecMeta,
    SQLDatabaseSpec,
    SSHCommand,
    SSHCommandSpec,
    VectorDatabaseSpec,
)
from .types import (
    RUNPOD_APP_AGENT,
    RUNPOD_APP_BROWSER,
    RUNPOD_APP_LLM_SERVER,
    RUNPOD_APP_SQL_DATABASE,
    RUNPOD_APP_VECTOR_DATABASE,
    RUNPOD_COMMAND_SSH,
    RUNPOD_DEPLOYMENT_SPEC,
    RUNPOD_KEEPALIVE_POLICY,
    RUNPOD_LLM_API,
    RUNPOD_RUN_RESULT,
    RUNPOD_STORAGE_NETWORK,
    RUNPOD_STORAGE_S3,
)
from .validation import ValidationError, validate_deployment


def norm(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def meta(node_id: str | None, display_name: str | None = None) -> SpecMeta:
    return SpecMeta(node_id=node_id, display_name=display_name)


class RunpodBrowserNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_BROWSER,)
    RETURN_NAMES = ("browser",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "browser": (["Neko", "Playwright"],),
                "placement": (["own_pod", "same_pod"],),
                "browser_engine": (["chromium", "firefox", "chrome"],),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, browser: str, placement: str, browser_engine: str, node_id: str | None = None):
        engine = "neko" if norm(browser) == "neko" else "playwright"
        if engine == "neko" and placement == "same_pod":
            raise ValidationError("Neko browser supports only own_pod placement.")
        values = {"BROWSER_KIND": engine}
        ports = []
        capabilities = []
        template_key = f"rp-browser-{engine}" if placement == "own_pod" else None
        if engine == "playwright":
            values["PLAYWRIGHT_MODE"] = "local" if placement == "same_pod" else "remote"
            if placement == "own_pod":
                values["PLAYWRIGHT_WS_ENDPOINT"] = "crag://browser/playwright"
                ports.append(PortSpec("playwright", 3000, "http", True))
            else:
                capabilities.append("playwright")
        else:
            values["NEKO_URL"] = "crag://browser/neko"
            ports.append(PortSpec("neko", 8080, "http", True))
        return (
            BrowserSpec(
                "browser",
                engine,
                placement,
                norm(browser_engine),
                RuntimeContract(EnvPatch(values), ports),
                capabilities,
                template_key,
                meta(node_id, browser),
            ),
        )


class RunpodLLMServerNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_LLM_SERVER,)
    RETURN_NAMES = ("llm_server",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "engine": (["Ollama", "vLLM"],),
                "model": ("STRING", {"default": ""}),
                "placement": (["own_pod"],),
                "api_auth_mode": (["none", "generated_token", "secret"],),
                "api_key_secret_name": ("STRING", {"default": ""}),
                "hf_token_secret_name": ("STRING", {"default": ""}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, model: str, placement: str, api_auth_mode: str, api_key_secret_name: str = "", hf_token_secret_name: str = "", node_id: str | None = None):
        llm_engine = "vllm" if norm(engine) == "vllm" else "ollama"
        if placement != "own_pod":
            raise ValidationError("LLM Server only supports own_pod placement in the MVP.")
        api_format = "openai" if llm_engine == "vllm" else "ollama"
        values = {"LLM_PROVIDER": llm_engine, "LLM_API_FORMAT": api_format, "LLM_MODEL": model}
        secrets = []
        if llm_engine == "ollama":
            values.update({"OLLAMA_HOST": "crag://llm/ollama", "OLLAMA_MODEL": model, "OPENAI_BASE_URL": "crag://llm/ollama/v1"})
            ports = [PortSpec("ollama", 11434, "http", True)]
        else:
            values.update({"OPENAI_BASE_URL": "crag://llm/vllm/v1", "OPENAI_MODEL": model})
            ports = [PortSpec("vllm", 8000, "http", True)]
        api_secret = None
        if api_auth_mode == "secret" and api_key_secret_name:
            api_secret = SecretRef(api_key_secret_name, "OPENAI_API_KEY")
            secrets.append(api_secret)
        elif api_auth_mode == "generated_token":
            values["OPENAI_API_KEY"] = "crag-generated-at-apply"
        hf_secret = SecretRef(hf_token_secret_name, "HF_TOKEN") if hf_token_secret_name else None
        if hf_secret:
            secrets.append(hf_secret)
        return (
            LLMServerSpec(
                "llm_server",
                llm_engine,
                model,
                "own_pod",
                api_format,
                RuntimeContract(EnvPatch(values, secrets), ports),
                [],
                f"rp-llm-{llm_engine}",
                hf_secret,
                api_secret,
                meta(node_id, engine),
            ),
        )


class RunpodLLMApiNode:
    CATEGORY = "Runpod/LLM"
    RETURN_TYPES = (RUNPOD_LLM_API,)
    RETURN_NAMES = ("llm_api",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "provider": (["Codex", "Claude", "Ollama Cloud"],),
                "model": ("STRING", {"default": ""}),
                "api_key_secret_name": ("STRING", {"default": ""}),
                "base_url_override": ("STRING", {"default": ""}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, provider: str, model: str, api_key_secret_name: str, base_url_override: str = "", node_id: str | None = None):
        provider_id = norm(provider)
        if provider_id == "ollama_cloud":
            api_format = "ollama"
            base_url = base_url_override or "https://ollama.com"
            env_var = "OLLAMA_API_KEY"
            values = {"LLM_PROVIDER": "ollama_cloud", "LLM_API_FORMAT": api_format, "OLLAMA_HOST": base_url, "OLLAMA_MODEL": model, "LLM_MODEL": model, "LLM_API_BASE_URL": base_url}
        elif provider_id == "claude":
            api_format = "anthropic"
            base_url = base_url_override or None
            env_var = "ANTHROPIC_API_KEY"
            values = {"LLM_PROVIDER": "claude", "LLM_API_FORMAT": api_format, "ANTHROPIC_MODEL": model, "LLM_MODEL": model}
        else:
            provider_id = "codex"
            api_format = "openai"
            base_url = base_url_override or None
            env_var = "OPENAI_API_KEY"
            values = {"LLM_PROVIDER": "codex", "LLM_API_FORMAT": api_format, "OPENAI_MODEL": model, "LLM_MODEL": model}
        if base_url:
            values["LLM_API_BASE_URL"] = base_url
            if api_format == "openai":
                values["OPENAI_BASE_URL"] = base_url
        secret = SecretRef(api_key_secret_name, env_var) if api_key_secret_name else None
        return (LLMApiSpec("llm_api", provider_id, model, api_format, base_url, RuntimeContract(EnvPatch(values, [secret] if secret else [])), secret, meta(node_id, provider)),)


class RunpodSQLDatabaseNode:
    CATEGORY = "Runpod/Database"
    RETURN_TYPES = (RUNPOD_APP_SQL_DATABASE,)
    RETURN_NAMES = ("sql_database",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "engine": (["Postgres", "MySQL", "SQLite"],),
                "database_name": ("STRING", {"default": "app"}),
                "username": ("STRING", {"default": "app"}),
                "password_secret_name": ("STRING", {"default": ""}),
                "sqlite_path": ("STRING", {"default": "/workspace/db/app.sqlite"}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, database_name: str, username: str, password_secret_name: str = "", sqlite_path: str = "/workspace/db/app.sqlite", node_id: str | None = None):
        db_engine = norm(engine)
        if db_engine == "sqlite":
            contract = RuntimeContract(EnvPatch({"DATABASE_KIND": "sqlite", "DATABASE_URL": f"sqlite:///{sqlite_path}"}))
            return (SQLDatabaseSpec("sql_database", "sqlite", "file_only", database_name, None, None, contract, None, meta(node_id, engine)),)
        secret = SecretRef(password_secret_name, "DATABASE_PASSWORD") if password_secret_name else None
        contract = RuntimeContract(
            EnvPatch({"DATABASE_KIND": db_engine, "DATABASE_URL": f"crag://sql/{db_engine}/{database_name}", "DATABASE_NAME": database_name, "DATABASE_USER": username}, [secret] if secret else []),
            [PortSpec(db_engine, 5432 if db_engine == "postgres" else 3306, "tcp", False)],
        )
        return (SQLDatabaseSpec("sql_database", db_engine, "own_pod", database_name, username, secret, contract, f"rp-db-{db_engine}", meta(node_id, engine)),)


class RunpodVectorDatabaseNode:
    CATEGORY = "Runpod/Database"
    RETURN_TYPES = (RUNPOD_APP_VECTOR_DATABASE,)
    RETURN_NAMES = ("vector_database",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"engine": (["Chroma", "Qdrant"],), "collection_name": ("STRING", {"default": "default"}), "persistence_path": ("STRING", {"default": "/workspace/vector"})}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, engine: str, collection_name: str, persistence_path: str = "/workspace/vector", node_id: str | None = None):
        vector_engine = norm(engine)
        port = 6333 if vector_engine == "qdrant" else 8000
        contract = RuntimeContract(EnvPatch({"VECTOR_KIND": vector_engine, "VECTOR_URL": f"crag://vector/{vector_engine}", "VECTOR_COLLECTION": collection_name}), [PortSpec(vector_engine, port, "http", True)])
        return (VectorDatabaseSpec("vector_database", vector_engine, "own_pod", collection_name, persistence_path, contract, f"rp-vector-{vector_engine}", meta(node_id, engine)),)


class RunpodAgentNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_AGENT,)
    RETURN_NAMES = ("agent",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"harness": (["Codex", "Claude", "OpenCode", "Hermes", "Pi"],), "model": ("STRING", {"default": ""}), "startup_mode": (["wait_for_commands", "auto_start", "manual"],), "workspace_path": ("STRING", {"default": "/workspace"})},
            "optional": {"browser": (RUNPOD_APP_BROWSER,), "llm_api": (RUNPOD_LLM_API,), "llm_server": (RUNPOD_APP_LLM_SERVER,), "sql_database": (RUNPOD_APP_SQL_DATABASE,), "vector_database": (RUNPOD_APP_VECTOR_DATABASE,)},
            "hidden": {"node_id": "UNIQUE_ID", "prompt": "PROMPT"},
        }

    def build(self, harness: str, model: str, startup_mode: str, workspace_path: str = "/workspace", browser=None, llm_api=None, llm_server=None, sql_database=None, vector_database=None, node_id: str | None = None, prompt: Any = None):
        if llm_api and llm_server:
            raise ValidationError("Agent accepts either llm_api or llm_server, not both.")
        capabilities = []
        for spec in (browser, llm_server):
            if spec and spec.materialization == "same_pod":
                if spec.kind == "llm_server":
                    raise ValidationError("LLM Server same_pod materialization is not supported in the MVP.")
                capabilities.extend(spec.required_image_capabilities)
        contract = RuntimeContract(EnvPatch({"AGENT_HARNESS": norm(harness), "AGENT_MODEL": model, "WORKSPACE_DIR": workspace_path}))
        return (AgentSpec("agent", norm(harness), model, startup_mode, workspace_path, browser, llm_api, llm_server, sql_database, vector_database, contract, capabilities, None, meta(node_id, harness)),)


class RunpodNetworkStorageNode:
    CATEGORY = "Runpod/Storage"
    RETURN_TYPES = (RUNPOD_STORAGE_NETWORK,)
    RETURN_NAMES = ("network_storage",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"network_volume_id": ("STRING", {"default": ""}), "mount_path": ("STRING", {"default": "/workspace"})}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, network_volume_id: str, mount_path: str = "/workspace", node_id: str | None = None):
        return (NetworkStorageSpec(network_volume_id, mount_path, meta(node_id, "Network Storage")),)


class RunpodS3StorageNode:
    CATEGORY = "Runpod/Storage"
    RETURN_TYPES = (RUNPOD_STORAGE_S3,)
    RETURN_NAMES = ("s3_storage",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"endpoint": ("STRING", {"default": ""}), "bucket": ("STRING", {"default": ""}), "region": ("STRING", {"default": ""}), "access_key_secret_name": ("STRING", {"default": ""}), "secret_key_secret_name": ("STRING", {"default": ""})}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, endpoint: str, bucket: str, region: str, access_key_secret_name: str, secret_key_secret_name: str, node_id: str | None = None):
        access = SecretRef(access_key_secret_name, "AWS_ACCESS_KEY_ID")
        secret = SecretRef(secret_key_secret_name, "AWS_SECRET_ACCESS_KEY")
        contract = RuntimeContract(EnvPatch({"S3_ENDPOINT": endpoint, "S3_BUCKET": bucket, "S3_REGION": region}, [access, secret]))
        return (S3StorageSpec(endpoint, bucket, region or None, access, secret, "S3", contract, meta(node_id, "S3 Storage")),)


class RunpodSSHCommandNode:
    CATEGORY = "Runpod/Command"
    RETURN_TYPES = (RUNPOD_COMMAND_SSH,)
    RETURN_NAMES = ("commands",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"command": ("STRING", {"multiline": True, "default": ""}), "phase": (["before_start", "after_start", "after_ready", "teardown"],), "order": ("INT", {"default": 0}), "failure_policy": (["fail", "continue", "retry"],), "retry_count": ("INT", {"default": 0, "min": 0})}, "optional": {"previous": (RUNPOD_COMMAND_SSH,)}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, command: str, phase: str, order: int, failure_policy: str, retry_count: int = 0, previous: SSHCommandSpec | None = None, node_id: str | None = None):
        commands = list(previous.commands) if previous else []
        commands.append(SSHCommand(command, phase, int(order), failure_policy, int(retry_count)))
        return (SSHCommandSpec(sorted(commands, key=lambda item: item.order), meta(node_id, "SSH Command")),)


class RunpodKeepAliveNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_KEEPALIVE_POLICY,)
    RETURN_NAMES = ("keep_alive",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mode": (["time", "turns", "cost", "manual"],), "action": (["stop", "terminate"],), "time_value": ("INT", {"default": 30, "min": 0}), "time_unit": (["seconds", "minutes", "hours"],), "turn_limit": ("INT", {"default": 0, "min": 0}), "cost_limit_usd": ("FLOAT", {"default": 0.0, "min": 0.0}), "idle_grace_seconds": ("INT", {"default": 0, "min": 0})}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, mode: str, action: str, time_value: int, time_unit: str, turn_limit: int, cost_limit_usd: float, idle_grace_seconds: int, node_id: str | None = None):
        multiplier = {"seconds": 1, "minutes": 60, "hours": 3600}[time_unit]
        return (KeepAlivePolicy(mode, action, int(time_value) * multiplier if mode == "time" else None, int(turn_limit) or None, float(cost_limit_usd) or None, int(idle_grace_seconds) or None, meta(node_id, "Keep Alive")),)


class RunpodPodNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_DEPLOYMENT_SPEC,)
    RETURN_NAMES = ("deployment",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"app": (RUNPOD_APP_AGENT,), "gpu_type_id": ("STRING", {"default": ""}), "gpu_count": ("INT", {"default": 1, "min": 0}), "cloud_type": (["auto", "SECURE", "COMMUNITY"],), "container_disk_gb": ("INT", {"default": 40, "min": 5}), "volume_gb": ("INT", {"default": 0, "min": 0}), "expose_public_ip": ("BOOLEAN", {"default": True}), "reuse_policy": (["reuse_matching", "always_create", "resume_stopped"],)},
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,), "s3_storage": (RUNPOD_STORAGE_S3,), "commands": (RUNPOD_COMMAND_SSH,), "keep_alive": (RUNPOD_KEEPALIVE_POLICY,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, app: AgentSpec, gpu_type_id: str = "", gpu_count: int = 1, cloud_type: str = "auto", container_disk_gb: int = 40, volume_gb: int = 0, expose_public_ip: bool = True, reuse_policy: str = "reuse_matching", network_storage=None, s3_storage=None, commands=None, keep_alive=None, node_id: str | None = None):
        hints = PodResourceHints(gpu_type_id or None, int(gpu_count), None if cloud_type == "auto" else cloud_type, int(container_disk_gb), int(volume_gb) or None, bool(expose_public_ip), int(gpu_count) == 0)
        deployment = DeploymentSpec(app, network_storage, s3_storage, commands, keep_alive, hints, reuse_policy, meta(node_id, "Pod"))
        validate_deployment(deployment, mode="plan", require_api_key=False)
        return (deployment,)


class RunpodRunNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_RUN_RESULT,)
    RETURN_NAMES = ("result",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"deployment": (RUNPOD_DEPLOYMENT_SPEC,), "mode": (["plan", "apply", "apply_and_wait", "stop", "terminate", "destroy"],), "on_error": (["stop_created", "terminate_created", "leave_running"],), "log_level": (["info", "debug"],)}, "hidden": {"prompt": "PROMPT"}}

    def run(self, deployment: DeploymentSpec, mode: str = "plan", on_error: str = "stop_created", log_level: str = "info", prompt: Any = None):
        if mode != "plan":
            from .runner import RunpodRunner

            result = RunpodRunner().run(deployment, mode=mode, prompt=prompt, on_error=on_error)
            return (json.dumps(result, indent=2, sort_keys=True),)
        plan = Planner().build(deployment, mode=mode, prompt=prompt)
        return (json.dumps(plan.to_dict(), indent=2, sort_keys=True),)


class RunpodLogsNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("logs", "saved_path")
    FUNCTION = "collect"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "run_id": ("STRING", {"default": ""}),
                "stream": (["both", "stdout", "stderr"],),
                "max_chars": ("INT", {"default": 20000, "min": 1000}),
                "save_copy": ("BOOLEAN", {"default": True}),
            }
        }

    def collect(self, run_id: str, stream: str = "both", max_chars: int = 20000, save_copy: bool = True):
        from .state_store import StateStore

        store = StateStore(default_state_path())
        text = collect_run_logs(store, run_id, stream=stream, max_chars=int(max_chars))
        saved_path = ""
        if save_copy and run_id:
            out_dir = store.path.parent / "logs" / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            saved = out_dir / f"collected-{stream}.log"
            saved.write_text(text)
            saved_path = str(saved)
        return (text, saved_path)


def collect_run_logs(store, run_id: str, *, stream: str, max_chars: int) -> str:
    if not run_id:
        return ""
    commands = store.list_commands(run_id)
    chunks: list[str] = []
    for command in commands:
        paths = []
        if stream in {"both", "stdout"} and command.get("stdout_path"):
            paths.append(("stdout", Path(command["stdout_path"])))
        if stream in {"both", "stderr"} and command.get("stderr_path"):
            paths.append(("stderr", Path(command["stderr_path"])))
        for label, path in paths:
            if not path.exists():
                continue
            chunks.append(f"===== {command['phase']} #{command['order_index']} {label} ({path}) =====")
            chunks.append(path.read_text(errors="replace"))
    text = "\n".join(chunks)
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


NODE_CLASSES = [
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodLLMServerNode,
    RunpodLLMApiNode,
    RunpodSQLDatabaseNode,
    RunpodVectorDatabaseNode,
    RunpodNetworkStorageNode,
    RunpodS3StorageNode,
    RunpodSSHCommandNode,
    RunpodKeepAliveNode,
    RunpodPodNode,
    RunpodRunNode,
    RunpodLogsNode,
]
