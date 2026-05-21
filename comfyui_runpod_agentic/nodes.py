from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import get_ssh_env_config
from .harnesses import CENTRAL_SKILLS_PATH, HARNESS_SUPPORT
from .planner import Planner
from .runner import default_state_path
from .runpod_options import optional_combo_or_string, runpod_dropdown_options
from .setup_commands import (
    container_snapshot_command,
    harness_install_command,
    language_runtime_install_command,
    local_sql_setup_command,
    package_install_command,
    skill_install_command,
)
from .specs import (
    AgentSpec,
    BrowserSpec,
    DeploymentSpec,
    EnvPatch,
    KeepAlivePolicy,
    LLMApiSpec,
    LLMServerSpec,
    MCPServer,
    MCPServerSpec,
    NetworkStorageSpec,
    PodResourceHints,
    PortSpec,
    RuntimeCommand,
    RuntimeContract,
    S3StorageSpec,
    SecretRef,
    SkillSource,
    SkillSpec,
    SpecMeta,
    SQLDatabaseSpec,
    SSHAccessPolicy,
    SSHCommand,
    SSHCommandSpec,
    VectorDatabaseSpec,
    WebTerminalSpec,
)
from .types import (
    RUNPOD_AGENT_SKILLS,
    RUNPOD_APP_AGENT,
    RUNPOD_APP_BROWSER,
    RUNPOD_APP_SQL_DATABASE,
    RUNPOD_APP_TERMINAL,
    RUNPOD_APP_VECTOR_DATABASE,
    RUNPOD_COMMAND_SSH,
    RUNPOD_DEPLOYMENT_SPEC,
    RUNPOD_KEEPALIVE_POLICY,
    RUNPOD_LLM,
    RUNPOD_MCP_SERVERS,
    RUNPOD_RUN_RESULT,
    RUNPOD_SSH_ACCESS_POLICY,
    RUNPOD_STORAGE_NETWORK,
    RUNPOD_STORAGE_S3,
)
from .validation import ValidationError, validate_deployment

SKILL_FRAMEWORKS = {
    "Superpowers": ("https://github.com/obra/superpowers.git", "skills"),
    "Superpowers Skills": ("https://github.com/obra/superpowers-skills.git", "skills"),
    "Anthropic Skills": ("https://github.com/anthropics/skills.git", "skills"),
    "Custom GitHub Repo": ("", "."),
}


def norm(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def meta(node_id: str | None, display_name: str | None = None) -> SpecMeta:
    return SpecMeta(node_id=node_id, display_name=display_name)


def generated_volume_id(node_id: str | None, volume_name: str | None) -> str:
    suffix = "".join(ch for ch in str(node_id or uuid.uuid4().hex[:8]).lower() if ch.isalnum())[:8] or uuid.uuid4().hex[:8]
    base = norm(volume_name or "crag-workspace").replace("_", "-")
    return f"{base}-{suffix}"


def default_resource_hints() -> PodResourceHints:
    return PodResourceHints(None, 1, None, 40, None, True, False)


def with_terminal_options(
    deployment: DeploymentSpec,
    *,
    gpu_type_id: str = "",
    gpu_count: int = 1,
    cloud_type: str = "auto",
    container_disk_gb: int = 40,
    volume_gb: int = 0,
    expose_public_ip: bool = True,
    reuse_policy: str = "reuse_matching",
    ssh_access: SSHAccessPolicy | None = None,
) -> DeploymentSpec:
    gpu_count_int = int(gpu_count)
    hints = PodResourceHints(None if gpu_count_int == 0 else gpu_type_id or None, gpu_count_int, None if cloud_type == "auto" else cloud_type, int(container_disk_gb), int(volume_gb) or None, bool(expose_public_ip), gpu_count_int == 0)
    return replace(deployment, resource_hints=hints, reuse_policy=reuse_policy, ssh_access=ssh_access or deployment.ssh_access)


class BrowserNode:
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
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, browser: str, placement: str, browser_engine: str, network_storage=None, node_id: str | None = None):
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
                network_storage,
                meta(node_id, browser),
            ),
        )


class WebTerminalNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_TERMINAL,)
    RETURN_NAMES = ("terminal",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shell": ("STRING", {"default": "/bin/bash"}),
                "port": ("INT", {"default": 7681, "min": 1, "max": 65535}),
                "host_port": ("INT", {"default": 7681, "min": 0, "max": 65535}),
                "auth_mode": (["password", "none"],),
                "username": ("STRING", {"default": "crag"}),
                "password": ("STRING", {"default": "crag-terminal"}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, shell: str = "/bin/bash", port: int = 7681, host_port: int = 7681, auth_mode: str = "password", username: str = "crag", password: str = "crag-terminal", node_id: str | None = None):
        terminal_port = int(port)
        local_host_port = int(host_port)
        terminal_shell = shell.strip() or "/bin/bash"
        terminal_auth = "none" if auth_mode == "none" else "password"
        terminal_user = username.strip() or "crag"
        terminal_password = password.strip()
        if terminal_auth == "password" and not terminal_password:
            raise ValidationError("Web Terminal password is required when auth_mode=password.")
        contract = RuntimeContract(
            EnvPatch(
                {
                    "CRAG_WEB_TERMINAL": "1",
                    "CRAG_WEB_TERMINAL_PORT": str(terminal_port),
                    "CRAG_WEB_TERMINAL_HOST_PORT": str(local_host_port),
                    "CRAG_WEB_TERMINAL_AUTH_MODE": terminal_auth,
                    "CRAG_WEB_TERMINAL_USERNAME": terminal_user,
                    "CRAG_WEB_TERMINAL_PASSWORD": terminal_password,
                    "CRAG_WEB_TERMINAL_SHELL": terminal_shell,
                }
            ),
            ports=[PortSpec("terminal", terminal_port, "http", True)],
            commands=[RuntimeCommand(web_terminal_command(terminal_port, terminal_shell, terminal_auth, terminal_user, terminal_password), "before_start", -40000, "continue", 0, "web_terminal")],
        )
        return (WebTerminalSpec("web_terminal", terminal_shell, terminal_port, local_host_port, terminal_auth, terminal_user, terminal_password, contract, meta(node_id, "Web Terminal")),)


def web_terminal_command(port: int, shell: str, auth_mode: str, username: str, password: str) -> str:
    credential = f" -c {shlex.quote(username + ':' + password)}" if auth_mode == "password" else ""
    terminal_command = shell.strip() or "/bin/bash"
    return "\n".join(
        [
            "set -e",
            "if ! command -v ttyd >/dev/null 2>&1; then",
            "  if command -v apt-get >/dev/null 2>&1; then",
            "    apt-get update",
            "    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl ttyd || true",
            "  fi",
            "fi",
            "if ! command -v ttyd >/dev/null 2>&1; then",
            "  curl -fsSL -o /usr/local/bin/ttyd https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64",
            "  chmod +x /usr/local/bin/ttyd",
            "fi",
            'mkdir -p "${WORKSPACE_DIR:-/workspace}/.runpod_agentic"',
            f"nohup ttyd -W -p {int(port)}{credential} /bin/bash -lc {shlex.quote(terminal_command)} > \"${{WORKSPACE_DIR:-/workspace}}/.runpod_agentic/ttyd.log\" 2>&1 &",
            'echo $! > "${WORKSPACE_DIR:-/workspace}/.runpod_agentic/ttyd.pid"',
        ]
    )


class LLMServerNode:
    CATEGORY = "Runpod/APIs"
    RETURN_TYPES = (RUNPOD_LLM,)
    RETURN_NAMES = ("llm",)
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
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, model: str, placement: str, api_auth_mode: str, api_key_secret_name: str = "", hf_token_secret_name: str = "", network_storage=None, node_id: str | None = None):
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
                network_storage,
                meta(node_id, engine),
            ),
        )


class LLMApiNode:
    CATEGORY = "Runpod/APIs"
    RETURN_TYPES = (RUNPOD_LLM,)
    RETURN_NAMES = ("llm",)
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
            secrets = [SecretRef(api_key_secret_name, "OLLAMA_API_KEY"), SecretRef(api_key_secret_name, "OLLAMA_CLOUD_API_KEY")] if api_key_secret_name else []
        elif provider_id == "claude":
            api_format = "anthropic"
            base_url = base_url_override or None
            env_var = "ANTHROPIC_API_KEY"
            values = {"LLM_PROVIDER": "claude", "LLM_API_FORMAT": api_format, "ANTHROPIC_MODEL": model, "LLM_MODEL": model}
            secrets = [SecretRef(api_key_secret_name, env_var)] if api_key_secret_name else []
        else:
            provider_id = "codex"
            api_format = "openai"
            base_url = base_url_override or None
            env_var = "OPENAI_API_KEY"
            values = {"LLM_PROVIDER": "codex", "LLM_API_FORMAT": api_format, "OPENAI_MODEL": model, "LLM_MODEL": model}
            secrets = [SecretRef(api_key_secret_name, env_var)] if api_key_secret_name else []
        if base_url:
            values["LLM_API_BASE_URL"] = base_url
            if api_format == "openai":
                values["OPENAI_BASE_URL"] = base_url
        secret = SecretRef(api_key_secret_name, env_var) if api_key_secret_name else None
        return (LLMApiSpec("llm_api", provider_id, model, api_format, base_url, RuntimeContract(EnvPatch(values, secrets)), secret, meta(node_id, provider)),)


class RemoteSQLDatabaseNode:
    CATEGORY = "Runpod/Database"
    RETURN_TYPES = (RUNPOD_APP_SQL_DATABASE,)
    RETURN_NAMES = ("sql_database",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "engine": (["Postgres", "MySQL"],),
                "connection_mode": (["own_pod", "env_only"],),
                "database_name": ("STRING", {"default": "app"}),
                "username": ("STRING", {"default": "app"}),
                "password_secret_name": ("STRING", {"default": ""}),
                "database_url_env_var": ("STRING", {"default": "DATABASE_URL"}),
            },
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, connection_mode: str, database_name: str, username: str, password_secret_name: str = "", database_url_env_var: str = "DATABASE_URL", network_storage=None, node_id: str | None = None):
        db_engine = norm(engine)
        if db_engine not in {"postgres", "mysql"}:
            raise ValidationError("Remote SQL Database supports Postgres and MySQL. Use Local SQL Database for SQLite.")
        mode = norm(connection_mode)
        if mode == "env_only":
            url_secret = SecretRef(database_url_env_var.strip() or "DATABASE_URL", "DATABASE_URL", "server_env")
            values = {
                "DATABASE_KIND": db_engine,
                "DATABASE_NAME": database_name,
                "DATABASE_USER": username,
            }
            contract = RuntimeContract(EnvPatch(values, [url_secret]))
            return (SQLDatabaseSpec("sql_database", db_engine, "env_only", database_name, username, url_secret, contract, None, None, meta(node_id, f"{engine} Env")),)
        secret = SecretRef(password_secret_name, "DATABASE_PASSWORD") if password_secret_name else None
        contract = RuntimeContract(
            EnvPatch({"DATABASE_KIND": db_engine, "DATABASE_URL": f"crag://sql/{db_engine}/{database_name}", "DATABASE_NAME": database_name, "DATABASE_USER": username}, [secret] if secret else []),
            [PortSpec(db_engine, 5432 if db_engine == "postgres" else 3306, "tcp", False)],
        )
        return (SQLDatabaseSpec("sql_database", db_engine, "own_pod", database_name, username, secret, contract, f"rp-db-{db_engine}", network_storage, meta(node_id, engine)),)


class LocalSQLDatabaseNode:
    CATEGORY = "Runpod/Database"
    RETURN_TYPES = (RUNPOD_APP_SQL_DATABASE,)
    RETURN_NAMES = ("sql_database",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "engine": (["SQLite"],),
                "database_name": ("STRING", {"default": "app"}),
                "database_path": ("STRING", {"default": "/workspace/db/app.sqlite"}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, database_name: str, database_path: str = "/workspace/db/app.sqlite", node_id: str | None = None):
        db_engine = norm(engine)
        if db_engine != "sqlite":
            raise ValidationError("Local SQL Database only supports SQLite.")
        path = database_path.strip() or "/workspace/db/app.sqlite"
        contract = RuntimeContract(
            EnvPatch({"DATABASE_KIND": "sqlite", "DATABASE_URL": f"sqlite:///{path}", "DATABASE_PATH": path, "DATABASE_NAME": database_name}),
            commands=[RuntimeCommand(local_sql_setup_command(path, database_name), "before_start", -20000, "fail", 0, "local_sql")],
        )
        return (SQLDatabaseSpec("sql_database", "sqlite", "file_only", database_name, None, None, contract, None, None, meta(node_id, "SQLite")),)


class VectorDatabaseNode:
    CATEGORY = "Runpod/Database"
    RETURN_TYPES = (RUNPOD_APP_VECTOR_DATABASE,)
    RETURN_NAMES = ("vector_database",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"engine": (["Chroma", "Qdrant"],), "collection_name": ("STRING", {"default": "default"}), "persistence_path": ("STRING", {"default": "/workspace/vector"})},
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, engine: str, collection_name: str, persistence_path: str = "/workspace/vector", network_storage=None, node_id: str | None = None):
        vector_engine = norm(engine)
        port = 6333 if vector_engine == "qdrant" else 8000
        contract = RuntimeContract(EnvPatch({"VECTOR_KIND": vector_engine, "VECTOR_URL": f"crag://vector/{vector_engine}", "VECTOR_COLLECTION": collection_name, "VECTOR_PERSISTENCE_PATH": persistence_path}), [PortSpec(vector_engine, port, "http", True)])
        return (VectorDatabaseSpec("vector_database", vector_engine, "own_pod", collection_name, persistence_path, contract, f"rp-vector-{vector_engine}", network_storage, meta(node_id, engine)),)


class MCPServerNode:
    CATEGORY = "Runpod/Agent"
    RETURN_TYPES = (RUNPOD_MCP_SERVERS,)
    RETURN_NAMES = ("mcp_servers",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "name": ("STRING", {"default": "filesystem"}),
                "transport": (["stdio", "http", "sse"],),
                "command": ("STRING", {"default": "npx"}),
                "args": ("STRING", {"default": "-y @modelcontextprotocol/server-filesystem /workspace"}),
                "url": ("STRING", {"default": ""}),
                "env_json": ("STRING", {"multiline": True, "default": "{}"}),
                "secret_env_names": ("STRING", {"default": ""}),
            },
            "optional": {"previous": (RUNPOD_MCP_SERVERS,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, name: str, transport: str, command: str, args: str, url: str = "", env_json: str = "{}", secret_env_names: str = "", previous: MCPServerSpec | None = None, node_id: str | None = None):
        server_name = name.strip()
        if not server_name:
            raise ValidationError("MCP server name is required.")
        transport_id = norm(transport)
        env = parse_json_object(env_json, "env_json")
        secrets = [SecretRef(secret_name.strip(), secret_name.strip(), "server_env") for secret_name in secret_env_names.split(",") if secret_name.strip()]
        if transport_id == "stdio":
            if not command.strip():
                raise ValidationError("MCP stdio transport requires a command.")
            server = MCPServer(server_name, "stdio", command.strip(), shlex.split(args or ""), None, env, secrets)
        else:
            if not url.strip():
                raise ValidationError("MCP http/sse transport requires a URL.")
            server = MCPServer(server_name, transport_id, None, [], url.strip(), env, secrets)
        servers = [*(previous.servers if previous else []), server]
        contract = mcp_runtime_contract(servers)
        return (MCPServerSpec(servers, contract, meta(node_id, "MCP Server")),)


def parse_json_object(raw: str, label: str) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{label} must be a JSON object.")
    return {str(key): str(value) for key, value in data.items()}


def mcp_runtime_contract(servers: list[MCPServer]) -> RuntimeContract:
    payload = {
        "mcpServers": {
            server.name: clean_mcp_server(server)
            for server in servers
        }
    }
    secrets = [secret for server in servers for secret in server.secret_refs]
    return RuntimeContract(
        EnvPatch({"MCP_SERVERS_JSON": json.dumps(payload, sort_keys=True)}, secrets),
    )


def clean_mcp_server(server: MCPServer) -> dict[str, Any]:
    if server.transport == "stdio":
        data: dict[str, Any] = {"transport": "stdio", "command": server.command, "args": server.args}
    else:
        data = {"transport": server.transport, "url": server.url}
    if server.env or server.secret_refs:
        data["env"] = {**server.env, **{secret.env_var: f"${{{secret.name}}}" for secret in server.secret_refs}}
    return data


def inferred_command_order(previous: SSHCommandSpec | None) -> int:
    if not previous or not previous.commands:
        return 0
    return max(command.order for command in previous.commands) + 100


class SkillNode:
    CATEGORY = "Runpod/Agent"
    RETURN_TYPES = (RUNPOD_AGENT_SKILLS,)
    RETURN_NAMES = ("skills",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "name": ("STRING", {"default": "repo-skill"}),
                "github_repo_url": ("STRING", {"default": "https://github.com/user/repo.git"}),
                "repo_path": ("STRING", {"default": "."}),
                "target_path": ("STRING", {"default": ""}),
                "git_ref": ("STRING", {"default": ""}),
            },
            "optional": {"previous": (RUNPOD_AGENT_SKILLS,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, name: str, github_repo_url: str, repo_path: str = ".", target_path: str = "", git_ref: str = "", previous: SkillSpec | None = None, node_id: str | None = None):
        skill_name = name.strip()
        repo_url = github_repo_url.strip()
        if not skill_name:
            raise ValidationError("Skill name is required.")
        if not repo_url.startswith(("https://github.com/", "git@github.com:")):
            raise ValidationError("Skill GitHub repo URL must start with https://github.com/ or git@github.com:.")
        destination = target_path.strip() or f"{CENTRAL_SKILLS_PATH}/{skill_name}"
        skill = SkillSource("skill", skill_name, repo_url, repo_path.strip() or ".", destination, git_ref.strip() or None)
        skills = [*(previous.skills if previous else []), skill]
        payload = {"skills": [skill_payload(item) for item in skills]}
        commands = [RuntimeCommand(skill_install_command(item), "before_start", -10000 + index, "fail", 0, f"skill:{item.name}") for index, item in enumerate(skills)]
        return (SkillSpec(skills, RuntimeContract(EnvPatch({"RUNPOD_AGENT_SKILLS_JSON": json.dumps(payload, sort_keys=True)}), commands=commands), meta(node_id, "Skill")),)


class SkillFrameworkNode:
    CATEGORY = "Runpod/Agent"
    RETURN_TYPES = (RUNPOD_AGENT_SKILLS,)
    RETURN_NAMES = ("skills",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "framework": (list(SKILL_FRAMEWORKS),),
                "custom_github_repo_url": ("STRING", {"default": ""}),
                "custom_repo_path": ("STRING", {"default": ""}),
                "target_root": ("STRING", {"default": CENTRAL_SKILLS_PATH}),
                "git_ref": ("STRING", {"default": ""}),
            },
            "optional": {"previous": (RUNPOD_AGENT_SKILLS,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, framework: str, custom_github_repo_url: str = "", custom_repo_path: str = "", target_root: str = CENTRAL_SKILLS_PATH, git_ref: str = "", previous: SkillSpec | None = None, node_id: str | None = None):
        repo_url, repo_path = SKILL_FRAMEWORKS[framework]
        if framework == "Custom GitHub Repo":
            repo_url = custom_github_repo_url.strip()
            repo_path = custom_repo_path.strip() or "."
        if not repo_url.startswith(("https://github.com/", "git@github.com:")):
            raise ValidationError("Skill framework GitHub repo URL must start with https://github.com/ or git@github.com:.")
        framework_name = norm(framework)
        skill = SkillSource("framework", framework_name, repo_url, repo_path, target_root.strip() or CENTRAL_SKILLS_PATH, git_ref.strip() or None)
        skills = [*(previous.skills if previous else []), skill]
        payload = {"skills": [skill_payload(item) for item in skills]}
        commands = [RuntimeCommand(skill_install_command(item), "before_start", -10000 + index, "fail", 0, f"skill:{item.name}") for index, item in enumerate(skills)]
        return (SkillSpec(skills, RuntimeContract(EnvPatch({"RUNPOD_AGENT_SKILLS_JSON": json.dumps(payload, sort_keys=True)}), commands=commands), meta(node_id, "Skill Framework")),)


def skill_payload(skill: SkillSource) -> dict[str, str]:
    payload = {
        "kind": skill.kind,
        "name": skill.name,
        "repo_url": skill.repo_url,
        "repo_path": skill.repo_path,
        "target_path": skill.target_path,
    }
    if skill.git_ref:
        payload["git_ref"] = skill.git_ref
    return payload


class AgentNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_AGENT,)
    RETURN_NAMES = ("agent",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"harness": (["Codex", "Claude", "OpenCode", "Hermes", "Pi"],), "model": ("STRING", {"default": ""}), "startup_mode": (["wait_for_commands", "auto_start", "manual"],), "workspace_path": ("STRING", {"default": "/workspace"}), "system_prompt": ("STRING", {"multiline": True, "default": ""})},
            "optional": {"browser": (RUNPOD_APP_BROWSER,), "llm": (RUNPOD_LLM,), "sql_database": (RUNPOD_APP_SQL_DATABASE,), "vector_database": (RUNPOD_APP_VECTOR_DATABASE,), "mcp_servers": (RUNPOD_MCP_SERVERS,), "skills": (RUNPOD_AGENT_SKILLS,), "terminal": (RUNPOD_APP_TERMINAL,)},
            "hidden": {"node_id": "UNIQUE_ID", "workflow_graph": "PROMPT"},
        }

    def build(self, harness: str, model: str, startup_mode: str, workspace_path: str = "/workspace", system_prompt: str = "", browser=None, llm=None, sql_database=None, vector_database=None, mcp_servers=None, skills=None, terminal=None, node_id: str | None = None, workflow_graph: Any = None):
        llm_api = llm if isinstance(llm, LLMApiSpec) else None
        llm_server = llm if isinstance(llm, LLMServerSpec) else None
        capabilities = []
        for spec in (browser, llm_server):
            if spec and spec.materialization == "same_pod":
                if spec.kind == "llm_server":
                    raise ValidationError("LLM Server same_pod materialization is not supported in the MVP.")
                capabilities.extend(spec.required_image_capabilities)
        harness_id = norm(harness)
        install_commands = []
        skip_terminal_only_manual = startup_mode == "manual" and terminal and not any((browser, llm, sql_database, vector_database, mcp_servers, skills))
        if not skip_terminal_only_manual and harness_id in {"codex", "claude", "opencode", "hermes", "pi"} and os.environ.get("CRAG_SKIP_HARNESS_INSTALL") != "1":
            install_commands.append(RuntimeCommand(harness_install_command(harness_id), "before_start", -30000, "continue" if terminal else "fail", 0, f"harness:{harness_id}"))
        env = {"AGENT_HARNESS": harness_id, "AGENT_MODEL": model, "AGENT_STARTUP_MODE": startup_mode, "AGENT_SYSTEM_PROMPT": system_prompt, "WORKSPACE_DIR": workspace_path}
        harness_warning = harness_capability_warning(harness_id, system_prompt)
        if harness_warning:
            env["CRAG_AGENT_WARNINGS"] = harness_warning
        contract = RuntimeContract(
            EnvPatch(env),
            commands=install_commands,
        )
        if mcp_servers:
            contract = RuntimeContract(
                EnvPatch({**contract.env.values, **mcp_servers.runtime_contract.env.values}, [*contract.env.secrets, *mcp_servers.runtime_contract.env.secrets]),
                files=mcp_servers.runtime_contract.files,
                commands=[*contract.commands, *mcp_servers.runtime_contract.commands],
            )
        if skills:
            contract = RuntimeContract(
                EnvPatch({**contract.env.values, **skills.runtime_contract.env.values}, [*contract.env.secrets, *skills.runtime_contract.env.secrets]),
                files={**contract.files, **skills.runtime_contract.files},
                commands=[*contract.commands, *skills.runtime_contract.commands],
            )
        if terminal:
            contract = RuntimeContract(
                EnvPatch({**contract.env.values, **terminal.runtime_contract.env.values}, [*contract.env.secrets, *terminal.runtime_contract.env.secrets]),
                ports=[*contract.ports, *terminal.runtime_contract.ports],
                files={**contract.files, **terminal.runtime_contract.files},
                commands=[*contract.commands, *terminal.runtime_contract.commands],
            )
        return (AgentSpec("agent", harness_id, model, startup_mode, workspace_path, system_prompt, browser, llm_api, llm_server, sql_database, vector_database, mcp_servers, skills, terminal, contract, capabilities, None, meta(node_id, harness)),)


def harness_capability_warning(harness_id: str, system_prompt: str) -> str:
    support = HARNESS_SUPPORT.get(harness_id)
    if support and system_prompt.strip() and not support.system_prompt:
        return f"{support.display_name} does not advertise system prompt support; AGENT_SYSTEM_PROMPT is available in the environment but the built-in launcher will not pass a guessed CLI flag."
    return ""


class NetworkStorageNode:
    CATEGORY = "Runpod/Storage"
    RETURN_TYPES = (RUNPOD_STORAGE_NETWORK,)
    RETURN_NAMES = ("network_storage",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        options = runpod_dropdown_options()
        return {
            "required": {
                "network_volume_id": ("STRING", {"default": ""}),
                "mount_path": ("STRING", {"default": "/workspace"}),
                "retention_policy": (["preserve", "delete_when_unused", "delete_with_deployment"],),
                "create_size_gb": ("INT", {"default": 0, "min": 0, "max": 4000}),
                "data_center_id": optional_combo_or_string(options.data_center_ids),
                "volume_name": ("STRING", {"default": "crag-workspace"}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, network_volume_id: str, mount_path: str = "/workspace", retention_policy: str = "preserve", create_size_gb: int = 0, data_center_id: str = "", volume_name: str = "crag-workspace", node_id: str | None = None):
        volume_id = network_volume_id.strip()
        size_gb = int(create_size_gb) or None
        if not volume_id and not size_gb:
            volume_id = generated_volume_id(node_id, volume_name)
        return (NetworkStorageSpec(volume_id, mount_path, retention_policy, size_gb, data_center_id.strip() or None, volume_name.strip() or None, meta(node_id, "Network Storage")),)


class S3StorageNode:
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


class SSHCommandNode:
    CATEGORY = "Runpod/Command"
    RETURN_TYPES = (RUNPOD_COMMAND_SSH,)
    RETURN_NAMES = ("commands",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"command": ("STRING", {"multiline": True, "default": ""}), "phase": (["before_start", "after_start", "after_ready", "teardown"],), "failure_policy": (["fail", "continue", "retry"],), "retry_count": ("INT", {"default": 0, "min": 0})}, "optional": {"previous": (RUNPOD_COMMAND_SSH,)}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, command: str, phase: str, failure_policy: str, retry_count: int = 0, previous: SSHCommandSpec | None = None, node_id: str | None = None, order: int | None = None):
        commands = list(previous.commands) if previous else []
        commands.append(SSHCommand(command, phase, inferred_command_order(previous), failure_policy, int(retry_count)))
        return (SSHCommandSpec(sorted(commands, key=lambda item: item.order), meta(node_id, "SSH Command")),)


class PackageNode:
    CATEGORY = "Runpod/Command"
    RETURN_TYPES = (RUNPOD_COMMAND_SSH,)
    RETURN_NAMES = ("commands",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "package_manager": (["apt", "npm", "pip"],),
                "packages": ("STRING", {"default": ""}),
                "failure_policy": (["fail", "continue", "retry"],),
                "retry_count": ("INT", {"default": 0, "min": 0}),
            },
            "optional": {"previous": (RUNPOD_COMMAND_SSH,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, package_manager: str, packages: str, failure_policy: str = "fail", retry_count: int = 0, previous: SSHCommandSpec | None = None, node_id: str | None = None, order: int | None = None):
        commands = list(previous.commands) if previous else []
        commands.append(SSHCommand(package_install_command(package_manager, packages), "before_start", inferred_command_order(previous), failure_policy, int(retry_count)))
        return (SSHCommandSpec(sorted(commands, key=lambda item: item.order), meta(node_id, "Package")),)


class LanguageRuntimeNode:
    CATEGORY = "Runpod/Command"
    RETURN_TYPES = (RUNPOD_COMMAND_SSH,)
    RETURN_NAMES = ("commands",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "runtime": (["nodejs", "python"],),
                "node_major_version": ("INT", {"default": 22, "min": 18}),
                "failure_policy": (["fail", "continue", "retry"],),
                "retry_count": ("INT", {"default": 0, "min": 0}),
            },
            "optional": {"previous": (RUNPOD_COMMAND_SSH,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, runtime: str, node_major_version: int = 22, failure_policy: str = "fail", retry_count: int = 0, previous: SSHCommandSpec | None = None, node_id: str | None = None, order: int | None = None):
        commands = list(previous.commands) if previous else []
        commands.append(SSHCommand(language_runtime_install_command(runtime, int(node_major_version)), "before_start", inferred_command_order(previous), failure_policy, int(retry_count)))
        return (SSHCommandSpec(sorted(commands, key=lambda item: item.order), meta(node_id, "Language Runtime")),)


class BuildContainerNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("result", "response", "errors", "compose_yaml", "saved_path")
    FUNCTION = "apply"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "deployment": (RUNPOD_DEPLOYMENT_SPEC,),
                "image_tag": ("STRING", {"default": "docker.io/user/crag-agent:latest"}),
                "container_runtime": (["nerdctl", "docker", "podman"],),
                "push_to_docker_hub": ("BOOLEAN", {"default": False}),
                "dockerhub_username_env": ("STRING", {"default": "DOCKERHUB_USERNAME"}),
                "dockerhub_token_env": ("STRING", {"default": "DOCKERHUB_TOKEN"}),
                "failure_policy": (["fail", "continue", "retry"],),
                "retry_count": ("INT", {"default": 0, "min": 0}),
                "project_name": ("STRING", {"default": "crag-build"}),
                "output_path": ("STRING", {"default": "artifacts/local-runtime/build-compose.yaml"}),
                "use_sudo": ("BOOLEAN", {"default": False}),
                "timeout_seconds": ("INT", {"default": 1800, "min": 1}),
            },
            "hidden": {"workflow_graph": "PROMPT"},
        }

    def apply(
        self,
        deployment: DeploymentSpec,
        image_tag: str,
        container_runtime: str = "nerdctl",
        push_to_docker_hub: bool = False,
        dockerhub_username_env: str = "DOCKERHUB_USERNAME",
        dockerhub_token_env: str = "DOCKERHUB_TOKEN",
        failure_policy: str = "fail",
        retry_count: int = 0,
        project_name: str = "crag-build",
        output_path: str = "artifacts/local-runtime/build-compose.yaml",
        use_sudo: bool = False,
        timeout_seconds: int = 1800,
        workflow_graph: Any = None,
    ):
        from .local_runtime import apply_local_runtime_plan, compose_yaml_for_plan, write_compose_file

        build_command = SSHCommand(container_snapshot_command(image_tag, container_runtime, bool(push_to_docker_hub), dockerhub_username_env, dockerhub_token_env), "after_ready", 50000, failure_policy, int(retry_count))
        commands = [*(deployment.ssh_commands.commands if deployment.ssh_commands else []), build_command]
        build_deployment = replace(deployment, ssh_commands=SSHCommandSpec(sorted(commands, key=lambda item: item.order), meta(None, "Build Container")), reuse_policy="always_create")
        plan = Planner().build(build_deployment, mode="plan", prompt=f"Build container {image_tag}", workflow_graph=workflow_graph)
        project = project_name.strip() or "crag-build"
        compose_yaml = compose_yaml_for_plan(plan, project_name=project)
        saved_path = write_compose_file(output_path, compose_yaml)
        old_sudo = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO")
        if use_sudo:
            os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"
        else:
            os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
        engine = {"nerdctl": "containerd", "docker": "docker", "podman": "podman"}[container_runtime]
        try:
            result, reused = apply_local_runtime_plan(engine, saved_path, project, plan, action="apply_and_wait", timeout_seconds=int(timeout_seconds))
        finally:
            if old_sudo is None:
                os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
            else:
                os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = old_sudo
        payload = json.loads(result.to_text())
        payload["reused"] = reused
        output = (json.dumps(payload, indent=2, sort_keys=True), result.stdout, result.stderr, compose_yaml, saved_path)
        return comfy_output(output, workflow_graph, self.RETURN_NAMES)


class KeepAliveNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_KEEPALIVE_POLICY,)
    RETURN_NAMES = ("keep_alive",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mode": (["time", "turns", "cost", "manual"],), "action": (["stop", "terminate"],), "time_value": ("INT", {"default": 30, "min": 0}), "time_unit": (["seconds", "minutes", "hours"],), "turn_limit": ("INT", {"default": 0, "min": 0}), "cost_limit_usd": ("FLOAT", {"default": 0.0, "min": 0.0}), "idle_grace_seconds": ("INT", {"default": 0, "min": 0}), "enforcement": (["both", "server_side", "pod_side"],)}, "hidden": {"node_id": "UNIQUE_ID"}}

    def build(self, mode: str, action: str, time_value: int, time_unit: str, turn_limit: int, cost_limit_usd: float, idle_grace_seconds: int, enforcement: str = "both", node_id: str | None = None):
        multiplier = {"seconds": 1, "minutes": 60, "hours": 3600}[time_unit]
        return (KeepAlivePolicy(mode, action, int(time_value) * multiplier if mode == "time" else None, int(turn_limit) or None, float(cost_limit_usd) or None, int(idle_grace_seconds) or None, enforcement, meta(node_id, "Keep Alive")),)


class SSHAccessNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_SSH_ACCESS_POLICY,)
    RETURN_NAMES = ("ssh_access",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (["runpod_proxy", "internal_sshd"],),
                "username": ("STRING", {"default": "root"}),
                "private_key_path": ("STRING", {"default": "~/.ssh/id_ed25519"}),
                "proxy_key_suffix": ("STRING", {"default": ""}),
                "internal_port": ("INT", {"default": 22, "min": 1, "max": 65535}),
                "install_internal_sshd": ("BOOLEAN", {"default": False}),
            },
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, mode: str, username: str, private_key_path: str, proxy_key_suffix: str = "", internal_port: int = 22, install_internal_sshd: bool = False, node_id: str | None = None):
        env_config = get_ssh_env_config()
        return (
            SSHAccessPolicy(
                mode,
                username,
                env_config.get("private_key_path") or private_key_path,
                proxy_key_suffix or env_config.get("proxy_suffix"),
                int(internal_port),
                bool(install_internal_sshd),
                meta(node_id, "SSH Access"),
            ),
        )


class DeployNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_DEPLOYMENT_SPEC,)
    RETURN_NAMES = ("deployment",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"app": (RUNPOD_APP_AGENT,)},
            "optional": {"network_storage": (RUNPOD_STORAGE_NETWORK,), "s3_storage": (RUNPOD_STORAGE_S3,), "commands": (RUNPOD_COMMAND_SSH,), "keep_alive": (RUNPOD_KEEPALIVE_POLICY,)},
            "hidden": {"node_id": "UNIQUE_ID"},
        }

    def build(self, app: AgentSpec, network_storage=None, s3_storage=None, commands=None, keep_alive=None, node_id: str | None = None):
        deployment = DeploymentSpec(app, network_storage, s3_storage, commands, keep_alive, SSHAccessPolicy(), default_resource_hints(), "reuse_matching", meta(node_id, "Deploy"))
        validate_deployment(deployment, mode="plan", require_api_key=False)
        return (deployment,)


class RunOnRunpodNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = (RUNPOD_RUN_RESULT, "STRING", "STRING")
    RETURN_NAMES = ("result", "response", "errors")
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **_kwargs):
        import time

        return time.time_ns()

    @classmethod
    def INPUT_TYPES(cls):
        options = runpod_dropdown_options()
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "deployment": (RUNPOD_DEPLOYMENT_SPEC,),
                "mode": (["plan", "apply", "apply_and_wait", "stop", "terminate"],),
                "gpu_type_id": optional_combo_or_string(options.gpu_type_ids),
                "gpu_count": ("INT", {"default": 1, "min": 0}),
                "cloud_type": (["auto", "SECURE", "COMMUNITY"],),
                "container_disk_gb": ("INT", {"default": 40, "min": 5}),
                "volume_gb": ("INT", {"default": 0, "min": 0}),
                "expose_public_ip": ("BOOLEAN", {"default": True}),
                "reuse_policy": (["reuse_matching", "always_create", "resume_stopped"],),
                "on_error": (["stop_created", "terminate_created", "leave_running"],),
                "log_level": (["info", "debug"],),
            },
            "optional": {"ssh_access": (RUNPOD_SSH_ACCESS_POLICY,)},
            "hidden": {"workflow_graph": "PROMPT"},
        }

    def run(
        self,
        deployment: DeploymentSpec,
        mode: str = "plan",
        prompt: str = "",
        gpu_type_id: str = "",
        gpu_count: int = 1,
        cloud_type: str = "auto",
        container_disk_gb: int = 40,
        volume_gb: int = 0,
        expose_public_ip: bool = True,
        reuse_policy: str = "reuse_matching",
        on_error: str = "stop_created",
        log_level: str = "info",
        ssh_access=None,
        workflow_graph: Any = None,
    ):
        progress = ComfyProgress()
        deployment = with_terminal_options(deployment, gpu_type_id=gpu_type_id, gpu_count=gpu_count, cloud_type=cloud_type, container_disk_gb=container_disk_gb, volume_gb=volume_gb, expose_public_ip=expose_public_ip, reuse_policy=reuse_policy, ssh_access=ssh_access)
        if mode != "plan":
            from .runner import RunpodRunner

            try:
                try:
                    runner = RunpodRunner(progress=progress)
                except TypeError:
                    runner = RunpodRunner()
                result = runner.run(deployment, mode=mode, prompt=prompt, workflow_graph=workflow_graph, on_error=on_error)
            except Exception as exc:
                result = {"status": "failed", "mode": mode, "error": str(exc), "errors": str(exc)}
            output = (json.dumps(result, indent=2, sort_keys=True), str(result.get("response") or ""), str(result.get("errors") or ""))
            return comfy_output(output, workflow_graph, self.RETURN_NAMES)
        plan = Planner().build(deployment, mode=mode, prompt=prompt, workflow_graph=workflow_graph)
        progress.set_total(max(1, len(plan.actions)))
        progress.update("plan")
        output = (json.dumps(plan.to_dict(), indent=2, sort_keys=True), "", "")
        return comfy_output(output, workflow_graph, self.RETURN_NAMES)


def comfy_output(result: tuple[str, ...], workflow_graph: Any, names: tuple[str, ...] | None = None):
    if workflow_graph is None:
        return result
    ui: dict[str, list[str]] = {"text": [result[0]]}
    if names is not None:
        for name, value in zip(names, result, strict=False):
            ui[name] = [value]
    return {"ui": ui, "result": result}


class ComfyProgress:
    def __init__(self):
        self.total = 1
        self.current = 0
        self.bar = None
        try:
            from comfy.utils import ProgressBar  # type: ignore

            self.bar = ProgressBar(self.total)
        except Exception:
            self.bar = None

    def set_total(self, total: int) -> None:
        self.total = max(1, int(total))
        self.current = 0
        if self.bar is None:
            return
        try:
            from comfy.utils import ProgressBar  # type: ignore

            self.bar = ProgressBar(self.total)
        except Exception:
            self.bar = None

    def update(self, _message: str = "") -> None:
        self.current = min(self.total, self.current + 1)
        if self.bar is None:
            return
        if hasattr(self.bar, "update_absolute"):
            self.bar.update_absolute(self.current, self.total)
        elif hasattr(self.bar, "update"):
            self.bar.update(1)


class StartupScriptNode:
    CATEGORY = "Runpod/Core"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("startup_script",)
    FUNCTION = "export"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"deployment": (RUNPOD_DEPLOYMENT_SPEC,), "prompt": ("STRING", {"multiline": True, "default": ""})}, "hidden": {"workflow_graph": "PROMPT"}}

    def export(self, deployment: DeploymentSpec, prompt: str = "", workflow_graph: Any = None):
        from .runner import startup_script_for_plan

        plan = Planner().build(deployment, mode="plan", prompt=prompt, workflow_graph=workflow_graph)
        return (startup_script_for_plan(plan),)


class ComposeYAMLNode:
    CATEGORY = "Runpod/Local"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("compose_yaml", "saved_path")
    FUNCTION = "export"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "deployment": (RUNPOD_DEPLOYMENT_SPEC,),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "project_name": ("STRING", {"default": "crag-local"}),
                "output_path": ("STRING", {"default": "artifacts/local-runtime/compose.yaml"}),
                "save_file": ("BOOLEAN", {"default": True}),
            },
            "hidden": {"workflow_graph": "PROMPT"},
        }

    def export(self, deployment: DeploymentSpec, prompt: str = "", project_name: str = "crag-local", output_path: str = "artifacts/local-runtime/compose.yaml", save_file: bool = True, workflow_graph: Any = None):
        from .local_runtime import compose_yaml_for_plan, write_compose_file

        plan = Planner().build(deployment, mode="plan", prompt=prompt, workflow_graph=workflow_graph)
        compose_yaml = compose_yaml_for_plan(plan, project_name=project_name.strip() or "crag-local")
        saved_path = write_compose_file(output_path, compose_yaml) if save_file else ""
        return (compose_yaml, saved_path)


class RunLocalContainersNode:
    CATEGORY = "Runpod/Local"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("result", "response", "errors", "compose_yaml", "saved_path")
    FUNCTION = "apply"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **_kwargs):
        import time

        return time.time_ns()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "deployment": (RUNPOD_DEPLOYMENT_SPEC,),
                "engine": (["containerd", "docker", "podman"],),
                "project_name": ("STRING", {"default": "crag-local"}),
                "output_path": ("STRING", {"default": "artifacts/local-runtime/compose.yaml"}),
                "action": (["save_only", "plan", "apply", "apply_and_wait", "stop", "terminate"],),
                "use_sudo": ("BOOLEAN", {"default": False}),
                "timeout_seconds": ("INT", {"default": 1800, "min": 1}),
                "response_role": ("STRING", {"default": "agent"}),
                "response_path": ("STRING", {"default": "/workspace/e2e/agent-skill-report.txt"}),
                "response_timeout_seconds": ("INT", {"default": 120, "min": 0}),
                "reuse_policy": (["reuse_matching", "always_create", "resume_stopped"],),
            },
            "hidden": {"workflow_graph": "PROMPT"},
        }

    def apply(
        self,
        deployment: DeploymentSpec,
        engine: str = "containerd",
        prompt: str = "",
        project_name: str = "crag-local",
        output_path: str = "artifacts/local-runtime/compose.yaml",
        action: str = "plan",
        use_sudo: bool = False,
        timeout_seconds: int = 1800,
        response_role: str = "agent",
        response_path: str = "/workspace/e2e/agent-skill-report.txt",
        response_timeout_seconds: int = 120,
        reuse_policy: str = "reuse_matching",
        workflow_graph: Any = None,
    ):
        import os

        from .local_runtime import (
            apply_local_runtime_plan,
            compose_yaml_for_plan,
            enforce_local_keep_alive,
            read_local_runtime_file,
            write_compose_file,
        )

        project = project_name.strip() or "crag-local"
        workflow_graph = populate_local_volume_ids(workflow_graph)
        deployment = replace(deployment, reuse_policy=reuse_policy)
        plan = Planner().build(deployment, mode="plan", prompt=prompt, workflow_graph=workflow_graph)
        compose_yaml = compose_yaml_for_plan(plan, project_name=project)
        saved_path = write_compose_file(output_path, compose_yaml)
        old_sudo = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO")
        if use_sudo:
            os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"
        else:
            os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
        try:
            result, reused = apply_local_runtime_plan(engine, saved_path, project, plan, action=action, timeout_seconds=int(timeout_seconds))
            response = ""
            response_errors = ""
            keep_alive_result = None
            if action in {"apply", "apply_and_wait"} and result.returncode == 0:
                keep_alive_result = enforce_local_keep_alive(engine, saved_path, project, plan, response_collected=False)
                if response_path.strip() and int(response_timeout_seconds) > 0:
                    read_result = read_local_runtime_file(engine, project, response_role.strip() or "agent", response_path.strip(), timeout_seconds=int(response_timeout_seconds))
                    response = read_result.stdout
                    response_errors = read_result.stderr
                    response_keep_alive_result = enforce_local_keep_alive(engine, saved_path, project, plan, response_collected=bool(response))
                    keep_alive_result = response_keep_alive_result or keep_alive_result
        finally:
            if old_sudo is None:
                os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
            else:
                os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = old_sudo
        result_payload = json.loads(result.to_text())
        result_payload["reused"] = reused
        terminal_urls = local_terminal_urls(plan) if action in {"apply", "apply_and_wait"} and result.returncode == 0 else {}
        if terminal_urls:
            result_payload["terminal_urls"] = terminal_urls
            terminal_auth = local_terminal_auth(plan)
            if terminal_auth:
                result_payload["terminal_auth"] = terminal_auth
        if keep_alive_result:
            result_payload["keep_alive"] = json.loads(keep_alive_result.to_text())
        errors = "\n".join(part for part in (result.stderr, response_errors, keep_alive_result.stderr if keep_alive_result else "") if part)
        output = (json.dumps(result_payload, indent=2, sort_keys=True), response, errors, compose_yaml, saved_path)
        return comfy_output(output, workflow_graph, self.RETURN_NAMES)


def populate_local_volume_ids(workflow_graph: Any) -> Any:
    if not isinstance(workflow_graph, dict):
        return workflow_graph
    for node_id, node in workflow_graph.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type") or node.get("type")
        if class_type not in {"NetworkStorage", "RunpodNetworkStorage"}:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("network_volume_id"):
            continue
        volume_name = str(inputs.get("volume_name") or "crag-workspace")
        inputs["network_volume_id"] = generated_volume_id(str(node_id), volume_name)
    return workflow_graph


def local_terminal_urls(plan) -> dict[str, str]:
    urls = {}
    for resource in plan.resources:
        env = resource.pod_input.get("env") or {}
        if env.get("CRAG_WEB_TERMINAL") != "1":
            continue
        host_port = int(env.get("CRAG_WEB_TERMINAL_HOST_PORT") or env.get("CRAG_WEB_TERMINAL_PORT") or 7681)
        if host_port > 0:
            urls[resource.role] = f"http://127.0.0.1:{host_port}"
    return urls


def local_terminal_auth(plan) -> dict[str, dict[str, str]]:
    auth = {}
    for resource in plan.resources:
        env = resource.pod_input.get("env") or {}
        if env.get("CRAG_WEB_TERMINAL") != "1" or env.get("CRAG_WEB_TERMINAL_AUTH_MODE") != "password":
            continue
        username = str(env.get("CRAG_WEB_TERMINAL_USERNAME") or "")
        password = str(env.get("CRAG_WEB_TERMINAL_PASSWORD") or "")
        if username and password:
            auth[resource.role] = {"username": username, "password": password}
    return auth


class LogsNode:
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
        from .runpod_client import RunpodClient
        from .ssh_client import SubprocessSSHClient
        from .state_store import StateStore

        store = StateStore(default_state_path())
        text = collect_run_logs(store, run_id, stream=stream, max_chars=int(max_chars), runpod_client=RunpodClient(), ssh_client=SubprocessSSHClient())
        saved_path = ""
        if save_copy and run_id:
            out_dir = store.path.parent / "logs" / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            saved = out_dir / f"collected-{stream}.log"
            saved.write_text(text)
            saved_path = str(saved)
        return (text, saved_path)


def collect_run_logs(store, run_id: str, *, stream: str, max_chars: int, runpod_client=None, ssh_client=None) -> str:
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
    if runpod_client and ssh_client:
        chunks.extend(collect_remote_agent_logs(store, run_id, runpod_client, ssh_client))
    text = "\n".join(chunks)
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def collect_remote_agent_logs(store, run_id: str, runpod_client, ssh_client) -> list[str]:
    from .ssh_client import extract_ssh_endpoint

    chunks: list[str] = []
    for resource in store.list_resources():
        if resource.get("run_id") != run_id or resource.get("role") != "agent" or not resource.get("runpod_pod_id"):
            continue
        try:
            pod = runpod_client.get_pod(resource["runpod_pod_id"])
            host, port = extract_ssh_endpoint(pod)
        except Exception as exc:
            chunks.append(f"===== remote agent logs unavailable ({resource.get('runpod_pod_id')}) =====")
            chunks.append(str(exc))
            continue
        for path in ("/workspace/.runpod_agentic/agent.log", "/workspace/.runpod_agentic/keepalive.log"):
            result = ssh_client.run(host, port, f"test -s {shlex.quote(path)} && cat {shlex.quote(path)}", timeout_seconds=20)
            if result.exit_code != 0 or not result.stdout:
                continue
            chunks.append(f"===== remote agent log ({path}) =====")
            chunks.append(result.stdout)
    return chunks


NODE_CLASSES = [
    AgentNode,
    BrowserNode,
    WebTerminalNode,
    LLMServerNode,
    LLMApiNode,
    LocalSQLDatabaseNode,
    RemoteSQLDatabaseNode,
    VectorDatabaseNode,
    MCPServerNode,
    SkillFrameworkNode,
    SkillNode,
    NetworkStorageNode,
    S3StorageNode,
    SSHCommandNode,
    PackageNode,
    LanguageRuntimeNode,
    BuildContainerNode,
    KeepAliveNode,
    SSHAccessNode,
    DeployNode,
    RunOnRunpodNode,
    StartupScriptNode,
    ComposeYAMLNode,
    RunLocalContainersNode,
    LogsNode,
]
