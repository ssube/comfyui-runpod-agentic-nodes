from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal

Materialization = Literal["own_pod", "same_pod", "file_only", "env_only", "config_only"]
StartupMode = Literal["wait_for_commands", "auto_start", "manual"]
CommandPhase = Literal["before_start", "after_start", "after_ready", "teardown"]
FailurePolicy = Literal["fail", "continue", "retry"]
KeepAliveMode = Literal["time", "turns", "cost", "manual"]
LimitAction = Literal["stop", "terminate"]
RunMode = Literal["plan", "apply", "apply_and_wait", "stop", "terminate", "destroy"]

SPEC_VERSION = "0.1"


@dataclass(frozen=True)
class SpecMeta:
    spec_version: str = SPEC_VERSION
    node_id: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class SecretRef:
    name: str
    env_var: str
    provider: Literal["runpod_secret", "server_env", "literal_for_dev_only"] = "runpod_secret"


@dataclass(frozen=True)
class PortSpec:
    name: str
    container_port: int
    protocol: Literal["http", "tcp"] = "http"
    public: bool = True


@dataclass(frozen=True)
class EnvPatch:
    values: dict[str, str] = field(default_factory=dict)
    secrets: list[SecretRef] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeContract:
    env: EnvPatch = field(default_factory=EnvPatch)
    ports: list[PortSpec] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BrowserSpec:
    kind: Literal["browser"]
    engine: Literal["neko", "playwright"]
    materialization: Materialization
    browser_engine: Literal["chromium", "firefox", "chrome"] | None
    runtime_contract: RuntimeContract
    required_image_capabilities: list[str]
    template_key: str | None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class LLMServerSpec:
    kind: Literal["llm_server"]
    engine: Literal["ollama", "vllm"]
    model: str
    materialization: Materialization
    api_format: Literal["openai", "ollama", "anthropic"]
    runtime_contract: RuntimeContract
    required_image_capabilities: list[str]
    template_key: str
    hf_token_secret: SecretRef | None = None
    api_key_secret: SecretRef | None = None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class LLMApiSpec:
    kind: Literal["llm_api"]
    provider: Literal["codex", "claude", "ollama_cloud"]
    model: str
    api_format: Literal["openai", "anthropic", "ollama"]
    base_url: str | None
    runtime_contract: RuntimeContract
    api_key_secret: SecretRef | None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class SQLDatabaseSpec:
    kind: Literal["sql_database"]
    engine: Literal["postgres", "mysql", "sqlite"]
    materialization: Materialization
    database_name: str
    username: str | None
    password_secret: SecretRef | None
    runtime_contract: RuntimeContract
    template_key: str | None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class VectorDatabaseSpec:
    kind: Literal["vector_database"]
    engine: Literal["chroma", "qdrant"]
    materialization: Materialization
    collection_name: str
    persistence_path: str
    runtime_contract: RuntimeContract
    template_key: str
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class AgentSpec:
    kind: Literal["agent"]
    harness: Literal["codex", "claude", "opencode", "hermes", "pi"]
    model: str
    startup_mode: StartupMode
    workspace_path: str
    browser: BrowserSpec | None = None
    llm_api: LLMApiSpec | None = None
    llm_server: LLMServerSpec | None = None
    sql_database: SQLDatabaseSpec | None = None
    vector_database: VectorDatabaseSpec | None = None
    runtime_contract: RuntimeContract = field(default_factory=RuntimeContract)
    required_image_capabilities: list[str] = field(default_factory=list)
    template_key: str | None = None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class NetworkStorageSpec:
    network_volume_id: str
    mount_path: str = "/workspace"
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class S3StorageSpec:
    endpoint: str
    bucket: str
    region: str | None
    access_key_secret: SecretRef
    secret_key_secret: SecretRef
    env_prefix: str = "S3"
    runtime_contract: RuntimeContract = field(default_factory=RuntimeContract)
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class SSHCommand:
    command: str
    phase: CommandPhase
    order: int
    failure_policy: FailurePolicy
    retry_count: int = 0


@dataclass(frozen=True)
class SSHCommandSpec:
    commands: list[SSHCommand]
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class KeepAlivePolicy:
    mode: KeepAliveMode
    action: LimitAction
    time_seconds: int | None = None
    turn_limit: int | None = None
    cost_limit_usd: float | None = None
    idle_grace_seconds: int | None = None
    meta: SpecMeta = field(default_factory=SpecMeta)


@dataclass(frozen=True)
class PodResourceHints:
    gpu_type_id: str | None
    gpu_count: int
    cloud_type: Literal["SECURE", "COMMUNITY"] | None
    container_disk_gb: int
    volume_gb: int | None
    expose_public_ip: bool
    cpu_only: bool = False


@dataclass(frozen=True)
class DeploymentSpec:
    primary_app: AgentSpec
    network_storage: NetworkStorageSpec | None
    s3_storage: S3StorageSpec | None
    ssh_commands: SSHCommandSpec | None
    keep_alive: KeepAlivePolicy | None
    resource_hints: PodResourceHints
    reuse_policy: Literal["reuse_matching", "always_create", "resume_stopped"]
    meta: SpecMeta = field(default_factory=SpecMeta)


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {k: to_plain(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    return value
