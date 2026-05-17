# ComfyUI Runpod Agentic Workflow Nodes — Implementation Spec

## 0. Purpose

Build a ComfyUI custom node package that lets users visually design, launch, connect, monitor, and shut down agentic systems running on Runpod.

The user-facing metaphor is simple:

- An **Agent** can have a browser, LLM, SQL database, vector database, storage, and setup commands.
- ComfyUI links are type-safe, so users can only connect compatible resources.
- The workflow behaves like a Terraform-style graph, but side effects happen only when the user presses ComfyUI **Run** and the terminal **Runpod Run** node executes.

## 1. Architecture Summary

### 1.1 Core rule

All nodes except `Runpod Run` must be declarative. They return typed Python spec objects or dictionaries. They must not create, stop, mutate, or SSH into Runpod resources.

`Runpod Run` is the only side-effecting node. It compiles the graph into a deployment plan, reconciles state with Runpod, creates/resumes/stops/terminates pods, runs SSH commands, writes runtime config, launches agents, and enforces keep-alive policy.

### 1.2 Resource materialization model

Every app/capability spec must include a `materialization` value:

```text
own_pod   -> create or reuse a dedicated Runpod pod
same_pod  -> install/run/enable inside the primary agent pod
file_only -> create a file/path/env contract; no service process
config_only/env_only -> inject env/secrets only; no pod
```

Examples:

```text
N.eko Browser       -> own_pod
Playwright Browser -> own_pod or same_pod
Ollama LLM Server  -> own_pod for MVP
vLLM LLM Server    -> own_pod for MVP
Postgres           -> own_pod
MySQL              -> own_pod
SQLite             -> file_only
Chroma             -> own_pod
Qdrant             -> own_pod
Codex/OpenAI API   -> env_only
Claude API         -> env_only
Ollama Cloud API   -> env_only
S3                 -> env_only/config_only
Network Storage    -> pod attachment
```

### 1.3 State model

Use the Runpod API as the source of truth for real infrastructure state.

Use a local SQLite ledger only as a convenience index/cache for:

- Comfy prompt/workflow ID to Runpod pod IDs.
- Desired spec hash to existing pod/resource instances.
- Command execution logs.
- Turn/cost/time keep-alive counters.
- Cleanup/reconciliation history.

The local DB must be rebuildable or safely recoverable by querying Runpod and matching managed resource names/prefixes.

## 2. Initial Node Set

```text
Runpod / Core
- Pod
- Run
- Keep Alive

Runpod / Apps
- Agent
- Browser
- LLM Server

Runpod / LLM
- LLM API

Runpod / Database
- SQL Database
- Vector Database

Runpod / Storage
- Network Storage
- S3 Storage

Runpod / Command
- SSH Command
```

## 3. ComfyUI Type Constants

Implement these as string constants in `types.py`.

```python
RUNPOD_APP_AGENT = "RUNPOD_APP_AGENT"
RUNPOD_APP_BROWSER = "RUNPOD_APP_BROWSER"
RUNPOD_APP_LLM_SERVER = "RUNPOD_APP_LLM_SERVER"
RUNPOD_LLM_API = "RUNPOD_LLM_API"
RUNPOD_APP_SQL_DATABASE = "RUNPOD_APP_SQL_DATABASE"
RUNPOD_APP_VECTOR_DATABASE = "RUNPOD_APP_VECTOR_DATABASE"
RUNPOD_STORAGE_NETWORK = "RUNPOD_STORAGE_NETWORK"
RUNPOD_STORAGE_S3 = "RUNPOD_STORAGE_S3"
RUNPOD_COMMAND_SSH = "RUNPOD_COMMAND_SSH"
RUNPOD_KEEPALIVE_POLICY = "RUNPOD_KEEPALIVE_POLICY"
RUNPOD_DEPLOYMENT_SPEC = "RUNPOD_DEPLOYMENT_SPEC"
RUNPOD_RUN_RESULT = "RUNPOD_RUN_RESULT"
```

Do not collapse all app nodes into a single generic `RUNPOD_APP_SPEC`. Specific types are important because `Agent` should have meaningful optional inputs such as `browser`, `sql_database`, `vector_database`, `llm_api`, and `llm_server`.

## 4. Python Package Layout

Target location:

```text
ComfyUI/custom_nodes/comfyui-runpod-agentic/
```

Recommended structure:

```text
comfyui-runpod-agentic/
├── __init__.py
├── pyproject.toml
├── requirements.txt
├── README.md
├── nodes.py
├── types.py
├── specs.py
├── planner.py
├── template_resolver.py
├── runpod_client.py
├── ssh_client.py
├── state_store.py
├── routes.py
├── runtime_contracts.py
├── validation.py
├── defaults/
│   ├── templates.example.yaml
│   └── config.example.yaml
├── tests/
│   ├── test_specs.py
│   ├── test_planner.py
│   ├── test_template_resolver.py
│   ├── test_state_store.py
│   └── test_runpod_client_mock.py
└── web/
    └── optional_frontend_extensions.js
```

## 5. Data Model

Use dataclasses or Pydantic models. Keep returned objects JSON-serializable wherever practical.

### 5.1 Common enums

```python
Materialization = Literal["own_pod", "same_pod", "file_only", "env_only", "config_only"]
StartupMode = Literal["wait_for_commands", "auto_start", "manual"]
CommandPhase = Literal["before_start", "after_start", "after_ready", "teardown"]
FailurePolicy = Literal["fail", "continue", "retry"]
KeepAliveMode = Literal["time", "turns", "cost", "manual"]
LimitAction = Literal["stop", "terminate"]
RunMode = Literal["plan", "apply", "apply_and_wait", "stop", "terminate", "destroy"]
```

### 5.2 Common structs

```python
@dataclass(frozen=True)
class SpecMeta:
    spec_version: str
    node_id: str | None
    display_name: str | None

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
    values: dict[str, str]
    secrets: list[SecretRef] = field(default_factory=list)

@dataclass(frozen=True)
class RuntimeContract:
    env: EnvPatch
    ports: list[PortSpec] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
```

### 5.3 App specs

```python
@dataclass(frozen=True)
class BrowserSpec:
    kind: Literal["browser"]
    engine: Literal["neko", "playwright"]
    materialization: Materialization
    browser_engine: Literal["chromium", "firefox", "chrome"] | None
    runtime_contract: RuntimeContract
    required_image_capabilities: list[str]
    template_key: str | None

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

@dataclass(frozen=True)
class LLMApiSpec:
    kind: Literal["llm_api"]
    provider: Literal["codex", "claude", "ollama_cloud"]
    model: str
    api_format: Literal["openai", "anthropic", "ollama"]
    base_url: str | None
    runtime_contract: RuntimeContract
    api_key_secret: SecretRef | None

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

@dataclass(frozen=True)
class VectorDatabaseSpec:
    kind: Literal["vector_database"]
    engine: Literal["chroma", "qdrant"]
    materialization: Materialization
    collection_name: str
    persistence_path: str
    runtime_contract: RuntimeContract
    template_key: str

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
```

### 5.4 Infrastructure specs

```python
@dataclass(frozen=True)
class NetworkStorageSpec:
    network_volume_id: str
    mount_path: str = "/workspace"

@dataclass(frozen=True)
class S3StorageSpec:
    endpoint: str
    bucket: str
    region: str | None
    access_key_secret: SecretRef
    secret_key_secret: SecretRef
    env_prefix: str = "S3"

@dataclass(frozen=True)
class SSHCommandSpec:
    commands: list[dict]

@dataclass(frozen=True)
class KeepAlivePolicy:
    mode: KeepAliveMode
    action: LimitAction
    time_seconds: int | None = None
    turn_limit: int | None = None
    cost_limit_usd: float | None = None
    idle_grace_seconds: int | None = None

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
```

## 6. Node Specifications

## 6.1 Agent Node

Category: `Runpod / Apps`

Output: `RUNPOD_APP_AGENT`

Required widgets:

```text
harness: Codex | Claude | OpenCode | Hermes | Pi
model: string
startup_mode: wait_for_commands | auto_start | manual
workspace_path: string, default /workspace
```

Optional typed inputs:

```text
browser: RUNPOD_APP_BROWSER
llm_api: RUNPOD_LLM_API
llm_server: RUNPOD_APP_LLM_SERVER
sql_database: RUNPOD_APP_SQL_DATABASE
vector_database: RUNPOD_APP_VECTOR_DATABASE
```

Rules:

- `Agent` must not create a pod.
- `Agent` is the composition point for optional browser, LLM, SQL, and vector resources.
- Allow zero or one of `llm_api` and `llm_server` for MVP. If both are connected, raise a validation error.
- If `browser.materialization == same_pod`, add the browser capability to `required_image_capabilities`.
- If `llm_server.materialization == same_pod`, add the LLM capability to `required_image_capabilities`; same-pod LLM is not part of MVP, so fail clearly unless explicitly enabled.
- Dependency specs requiring `own_pod` should be nested inside `AgentSpec`; the planner will materialize them before the primary agent pod.

Pseudo-node shape:

```python
class RunpodAgentNode:
    CATEGORY = "Runpod/Apps"
    RETURN_TYPES = (RUNPOD_APP_AGENT,)
    RETURN_NAMES = ("agent",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "harness": (["Codex", "Claude", "OpenCode", "Hermes", "Pi"],),
                "model": ("STRING", {"default": ""}),
                "startup_mode": (["wait_for_commands", "auto_start", "manual"],),
                "workspace_path": ("STRING", {"default": "/workspace"}),
            },
            "optional": {
                "browser": (RUNPOD_APP_BROWSER,),
                "llm_api": (RUNPOD_LLM_API,),
                "llm_server": (RUNPOD_APP_LLM_SERVER,),
                "sql_database": (RUNPOD_APP_SQL_DATABASE,),
                "vector_database": (RUNPOD_APP_VECTOR_DATABASE,),
            },
            "hidden": {
                "node_id": "UNIQUE_ID",
                "prompt": "PROMPT",
            },
        }
```

## 6.2 Browser Node

Category: `Runpod / Apps`

Output: `RUNPOD_APP_BROWSER`

Widgets:

```text
browser: Neko | Playwright
placement: own_pod | same_pod
browser_engine: chromium | firefox | chrome
```

Rules:

- N.eko only supports `own_pod` in the MVP.
- Playwright supports `own_pod` and `same_pod`.
- `same_pod` Playwright requires the chosen Agent template to include the `playwright` image capability.
- `own_pod` Playwright creates a browser server pod and injects `PLAYWRIGHT_WS_ENDPOINT` into the agent runtime contract.

Contracts:

```text
Playwright same_pod:
  BROWSER_KIND=playwright
  PLAYWRIGHT_MODE=local

Playwright own_pod:
  BROWSER_KIND=playwright
  PLAYWRIGHT_MODE=remote
  PLAYWRIGHT_WS_ENDPOINT=<resolved after browser pod starts>

Neko own_pod:
  BROWSER_KIND=neko
  NEKO_URL=<resolved after browser pod starts>
```

## 6.3 LLM Server Node

Category: `Runpod / Apps`

Output: `RUNPOD_APP_LLM_SERVER`

Widgets:

```text
engine: Ollama | vLLM
model: string
placement: own_pod
api_auth_mode: none | generated_token | secret
api_key_secret_name: string optional
hf_token_secret_name: string optional
```

Rules:

- MVP materialization is `own_pod` only.
- `Ollama` default container port: `11434`.
- `Ollama` should expose both native Ollama API config and OpenAI-compatible config where useful.
- `vLLM` default container port: `8000`.
- `vLLM` should normalize as an OpenAI-compatible endpoint.
- If `hf_token_secret_name` is set, pass it as a Runpod secret reference, not a literal workflow value.

Contracts:

```text
Ollama own_pod:
  LLM_PROVIDER=ollama
  LLM_API_FORMAT=ollama
  OLLAMA_HOST=<resolved host>
  OLLAMA_MODEL=<model>
  OPENAI_BASE_URL=<resolved host>/v1
  OPENAI_API_KEY=ollama or generated token

vLLM own_pod:
  LLM_PROVIDER=vllm
  LLM_API_FORMAT=openai
  OPENAI_BASE_URL=<resolved host>/v1
  OPENAI_API_KEY=<generated or secret token>
  OPENAI_MODEL=<model>
```

Template keys:

```text
rp-llm-ollama
rp-llm-vllm
```

## 6.4 LLM API Node

Category: `Runpod / LLM`

Output: `RUNPOD_LLM_API`

Widgets:

```text
provider: Codex | Claude | Ollama Cloud
model: string
api_key_secret_name: string
base_url_override: string optional
```

Rules:

- This node never creates a pod.
- It emits env/secrets only.
- `Codex` should be represented as an OpenAI-compatible provider in the runtime contract.
- `Claude` should be represented as an Anthropic provider in the runtime contract.
- `Ollama Cloud` should be represented as an Ollama provider with remote host `https://ollama.com` unless overridden.

Contracts:

```text
Codex/OpenAI:
  LLM_PROVIDER=codex
  LLM_API_FORMAT=openai
  OPENAI_API_KEY={{ RUNPOD_SECRET_x }}
  OPENAI_MODEL=<model>

Claude:
  LLM_PROVIDER=claude
  LLM_API_FORMAT=anthropic
  ANTHROPIC_API_KEY={{ RUNPOD_SECRET_x }}
  ANTHROPIC_MODEL=<model>

Ollama Cloud:
  LLM_PROVIDER=ollama_cloud
  LLM_API_FORMAT=ollama
  OLLAMA_API_KEY={{ RUNPOD_SECRET_x }}
  OLLAMA_HOST=https://ollama.com
  OLLAMA_MODEL=<model>
```

## 6.5 SQL Database Node

Category: `Runpod / Database`

Output: `RUNPOD_APP_SQL_DATABASE`

Widgets:

```text
engine: Postgres | MySQL | SQLite
database_name: string
username: string
password_secret_name: string optional
sqlite_path: string, default /workspace/db/app.sqlite
```

Rules:

```text
Postgres -> own_pod
MySQL    -> own_pod
SQLite   -> file_only
```

Contracts:

```text
Postgres:
  DATABASE_KIND=postgres
  DATABASE_URL=<resolved after pod starts>

MySQL:
  DATABASE_KIND=mysql
  DATABASE_URL=<resolved after pod starts>

SQLite:
  DATABASE_KIND=sqlite
  DATABASE_URL=sqlite:////workspace/db/app.sqlite
```

## 6.6 Vector Database Node

Category: `Runpod / Database`

Output: `RUNPOD_APP_VECTOR_DATABASE`

Widgets:

```text
engine: Chroma | Qdrant
collection_name: string
persistence_path: string, default /workspace/vector
```

Rules:

- Chroma and Qdrant are both `own_pod` for MVP.
- Add embedded Chroma later only after the basic service-pod path is stable.

Contracts:

```text
Chroma:
  VECTOR_KIND=chroma
  VECTOR_URL=<resolved after pod starts>
  VECTOR_COLLECTION=<collection_name>

Qdrant:
  VECTOR_KIND=qdrant
  VECTOR_URL=<resolved after pod starts>
  VECTOR_COLLECTION=<collection_name>
```

## 6.7 Network Storage Node

Category: `Runpod / Storage`

Output: `RUNPOD_STORAGE_NETWORK`

Widgets:

```text
network_volume_id: string
mount_path: string, default /workspace
```

Rules:

- Network volume attachment must be resolved before pod creation.
- Network Storage connects to the `Pod` node, not directly to `Agent`.

## 6.8 S3 Storage Node

Category: `Runpod / Storage`

Output: `RUNPOD_STORAGE_S3`

Widgets:

```text
endpoint: string
bucket: string
region: string
access_key_secret_name: string
secret_key_secret_name: string
```

Rules:

- S3 is env/config only by default.
- Do not mount S3 with `s3fs` in MVP unless explicitly added later.
- Never put literal access keys in the ComfyUI workflow JSON.

Contracts:

```text
S3_ENDPOINT=<endpoint>
S3_BUCKET=<bucket>
S3_REGION=<region>
AWS_ACCESS_KEY_ID={{ RUNPOD_SECRET_x }}
AWS_SECRET_ACCESS_KEY={{ RUNPOD_SECRET_y }}
```

## 6.9 SSH Command Node

Category: `Runpod / Command`

Output: `RUNPOD_COMMAND_SSH`

Widgets:

```text
command: multiline string
phase: before_start | after_start | after_ready | teardown
order: integer
failure_policy: fail | continue | retry
retry_count: integer, default 0
```

Optional input:

```text
previous: RUNPOD_COMMAND_SSH
```

Rules:

- The node returns a command chain.
- It does not SSH by itself.
- `Pod` accepts the final command chain.
- Use `phase=before_start` for commands that must run after pod creation but before the agent harness starts.

## 6.10 Keep Alive Node

Category: `Runpod / Core`

Output: `RUNPOD_KEEPALIVE_POLICY`

Widgets:

```text
mode: time | turns | cost | manual
action: stop | terminate
time_value: integer
time_unit: seconds | minutes | hours
turn_limit: integer
cost_limit_usd: float
idle_grace_seconds: integer
```

Rules:

- Time policy should be pushed into Runpod as `stopAfter` or `terminateAfter` where possible.
- Turns policy requires the runtime supervisor or agent launcher to report turn completion back to ComfyUI server routes.
- Cost policy is estimated from Runpod pod cost/hour and runtime unless exact billing data is added later.

## 6.11 Pod Node

Category: `Runpod / Core`

Output: `RUNPOD_DEPLOYMENT_SPEC`

Required input:

```text
app: RUNPOD_APP_AGENT
```

Optional typed inputs:

```text
network_storage: RUNPOD_STORAGE_NETWORK
s3_storage: RUNPOD_STORAGE_S3
commands: RUNPOD_COMMAND_SSH
keep_alive: RUNPOD_KEEPALIVE_POLICY
```

Widgets:

```text
gpu_type_id: string
gpu_count: integer
cloud_type: SECURE | COMMUNITY | auto
container_disk_gb: integer
volume_gb: integer optional
expose_public_ip: boolean
reuse_policy: reuse_matching | always_create | resume_stopped
```

Rules:

- The node is called `Pod`, but it outputs `RUNPOD_DEPLOYMENT_SPEC` because an Agent may contain dependent pod-owning resources.
- The primary pod is the agent pod.
- Own-pod dependencies nested inside the Agent spec are materialized by the planner before the primary agent pod.

## 6.12 Run Node

Category: `Runpod / Core`

Output: `RUNPOD_RUN_RESULT`

Required input:

```text
deployment: RUNPOD_DEPLOYMENT_SPEC
```

Widgets:

```text
mode: plan | apply | apply_and_wait | stop | terminate | destroy
on_error: stop_created | terminate_created | leave_running
log_level: info | debug
```

Rules:

- Set `OUTPUT_NODE = True`.
- This is the only side-effecting node.
- In `plan` mode, do not call create/resume/stop/terminate mutations and do not run SSH.
- In `apply` mode, create/resume resources, run commands, launch the agent, and return immediately after launch.
- In `apply_and_wait` mode, monitor until agent completion or keep-alive limit.
- In `stop`, `terminate`, and `destroy` modes, operate on previously managed resources found via state ledger and Runpod reconciliation.

## 7. Template Strategy

Use Runpod templates with prebuilt/cached applications. Do not install core harnesses during normal startup.

### 7.1 Principle

```text
Core harnesses       -> baked into templates
Common runtime deps  -> baked into shared base image
LLM/browser/db apps  -> baked into app-specific templates
User customization   -> SSH Command nodes
One-off installs     -> allowed only as explicit SSH commands
```

### 7.2 Recommended templates

```text
rp-agent-base
rp-agent-codex
rp-agent-claude
rp-agent-opencode
rp-agent-hermes
rp-agent-pi
rp-agent-codex-playwright       # optional if Playwright is not universal
rp-agent-claude-playwright      # optional if Playwright is not universal
rp-agent-opencode-playwright    # optional if Playwright is not universal

rp-browser-playwright
rp-browser-neko

rp-llm-ollama
rp-llm-vllm

rp-db-postgres
rp-db-mysql

rp-vector-chroma
rp-vector-qdrant
```

### 7.3 Shared agent base

Base image recommendation:

```text
FROM runpod/pytorch:<pinned-version>
```

Install/cache:

```text
python tooling
node tooling
git, curl, jq, ripgrep, tmux, openssh
uv / pipx / npm / pnpm as needed
runpod workflow runtime supervisor
workspace conventions
optional Playwright deps if we choose universal same-pod browser support
```

Do not use `latest` tags in production templates. Pin image tags.

### 7.4 Startup command

Agent templates should not start the agent immediately.

Preferred start command:

```bash
python -m runpod_agentic_runtime.supervisor
```

MVP fallback:

```bash
sleep infinity
```

The `Runpod Run` node sequence should be:

```text
1. Create/resume pod.
2. Wait for SSH/readiness.
3. Run before_start SSH commands.
4. Write runtime config files.
5. Launch agent harness through supervisor or SSH.
```

### 7.5 Template resolver

Implement `template_resolver.py`.

Inputs:

```text
Agent harness
Required embedded capabilities
App kind
Materialization mode
```

Output:

```text
template_id
image_name optional
ports
startup command hints
```

Use config file:

```yaml
agent_templates:
  opencode:
    default: rp-agent-opencode
    capabilities:
      playwright: rp-agent-opencode-playwright
  codex:
    default: rp-agent-codex

app_templates:
  browser:
    playwright: rp-browser-playwright
    neko: rp-browser-neko
  llm_server:
    ollama: rp-llm-ollama
    vllm: rp-llm-vllm
  sql_database:
    postgres: rp-db-postgres
    mysql: rp-db-mysql
  vector_database:
    chroma: rp-vector-chroma
    qdrant: rp-vector-qdrant
```

If a required capability is missing, fail clearly. Do not silently install large dependencies at runtime.

## 8. Planner Behavior

Implement `planner.py`.

### 8.1 Input

```python
DeploymentSpec
```

### 8.2 Output

A plan object containing ordered actions:

```text
CREATE_OR_RESUME dependency pods
WAIT dependency readiness
RESOLVE dependency env contracts
CREATE_OR_RESUME primary agent pod
WAIT primary SSH readiness
RUN before_start SSH commands
WRITE runtime config files
LAUNCH primary agent
RUN after_start SSH commands
MONITOR keep_alive
```

### 8.3 Dependency extraction

From `deployment.primary_app`, collect:

```text
browser
llm_server
llm_api
sql_database
vector_database
s3_storage
network_storage
```

Split by materialization:

```text
own_pod   -> resource node in deployment plan
same_pod  -> required capability on primary agent pod
file_only -> runtime file/env contract
config/env_only -> env/secrets contract
```

### 8.4 Creation order

```text
1. Own-pod LLM server, SQL database, vector database, browser dependencies.
2. Wait for dependency readiness.
3. Resolve dependency endpoints into env contracts.
4. Create/resume primary agent pod with all available env vars and secrets.
5. Wait for SSH.
6. Run before_start commands.
7. Write runtime config.
8. Launch agent.
```

### 8.5 Runtime config files

Write these into the primary pod:

```text
/workspace/.runpod_agentic/resources.json
/workspace/.runpod_agentic/session.env
/workspace/.runpod_agentic/commands.json
```

`resources.json` should include non-secret resource metadata and resolved service endpoints.

`session.env` should include environment exports for shell-based agent harnesses. Do not write literal secret values unless they are already present inside the pod via Runpod secret substitution.

## 9. Runpod API Integration

Implement `runpod_client.py`.

### 9.1 Authentication

Read API key from server-side config or environment:

```text
RUNPOD_API_KEY
```

Never expose the Runpod API key as a ComfyUI workflow widget value.

### 9.2 Required operations

Implement GraphQL wrappers:

```python
create_or_deploy_pod(input: dict) -> dict
get_pod(pod_id: str) -> dict
list_pods() -> list[dict]
stop_pod(pod_id: str) -> dict
resume_pod(pod_id: str) -> dict
terminate_pod(pod_id: str) -> None
```

Use whichever Runpod mutation is most appropriate after implementation verification:

```text
podFindAndDeployOnDemand
podRentInterruptable
podResume
podStop
podTerminate
```

Pod creation inputs should support:

```text
templateId
imageName
name
env
ports
startSsh
networkVolumeId
volumeMountPath
containerDiskInGb
volumeInGb
gpuTypeId
gpuCount
cloudType
stopAfter
terminateAfter
```

### 9.3 Naming and tagging

Every managed pod name should include:

```text
crag-<short_workflow_hash>-<role>-<short_node_id>-<short_desired_hash>
```

`crag` = Comfy Runpod Agentic Graph.

Add env vars at creation time:

```text
RUNPOD_MANAGED_BY=comfyui-runpod-agentic
CRAG_RUN_ID=<run_id>
CRAG_WORKFLOW_HASH=<hash>
CRAG_NODE_ID=<node_id>
CRAG_ROLE=agent|browser|llm|sql|vector
CRAG_DESIRED_HASH=<hash>
```

Do not rely on updating pod env vars after creation. If dependency information is discovered after a pod exists, write it to runtime config files via SSH instead.

## 10. SSH Implementation

Implement `ssh_client.py`.

### 10.1 Recommended library

Use one of:

```text
asyncssh
paramiko
```

Prefer `asyncssh` if the rest of the server route implementation is async. Otherwise `paramiko` is acceptable for MVP.

### 10.2 Config

Server-side config:

```yaml
ssh:
  username: root
  private_key_path: ~/.ssh/id_ed25519
  connect_timeout_seconds: 120
  command_timeout_seconds: 1800
```

### 10.3 Endpoint discovery

Implement:

```python
def extract_ssh_endpoint(pod: dict) -> tuple[str, int]:
    ...
```

Use Runpod pod runtime/ports data to find the TCP mapping for internal port 22. Add fixture-based tests for the shapes returned by the Runpod API.

### 10.4 Command execution

For each SSH command:

```text
- create command record in state DB
- execute command
- stream/capture stdout and stderr
- set exit code
- apply failure policy
- write logs to local files under user/runpod-agentic/logs/<run_id>/
```

## 11. State Store

Implement `state_store.py` using SQLite for MVP.

Path:

```text
ComfyUI/user/runpod-agentic/state.sqlite
```

Tables:

```sql
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  prompt_id TEXT,
  workflow_hash TEXT,
  created_at TEXT,
  updated_at TEXT,
  mode TEXT,
  status TEXT,
  deployment_hash TEXT
);

CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  node_id TEXT,
  role TEXT,
  desired_hash TEXT,
  runpod_pod_id TEXT,
  runpod_template_id TEXT,
  name TEXT,
  status TEXT,
  cost_per_hr REAL,
  created_at TEXT,
  last_seen_at TEXT,
  stop_after TEXT,
  terminate_after TEXT
);

CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  phase TEXT,
  order_index INTEGER,
  command_hash TEXT,
  status TEXT,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  stdout_path TEXT,
  stderr_path TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  timestamp TEXT,
  event_type TEXT,
  message TEXT,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS counters (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  counter_type TEXT,
  value REAL,
  updated_at TEXT
);
```

Before every apply/stop/terminate/destroy operation:

```text
1. Load local ledger.
2. Query Runpod API.
3. Reconcile statuses.
4. Detect orphaned managed pods by name prefix and env tags where available.
5. Update local ledger.
```

If SQLite is missing or corrupt, the extension should still be able to list currently managed pods by querying Runpod and matching the managed pod name prefix.

## 12. Server Routes

Implement `routes.py` using ComfyUI server routes.

Routes:

```text
GET  /runpod-agentic/resources
GET  /runpod-agentic/runs
GET  /runpod-agentic/runs/{run_id}
POST /runpod-agentic/pod/stop
POST /runpod-agentic/pod/resume
POST /runpod-agentic/pod/terminate
POST /runpod-agentic/run/cleanup
POST /runpod-agentic/run/{run_id}/turn
```

Route behavior:

```text
/resources -> list local + reconciled Runpod resources
/runs -> list recent runs
/pod/stop -> stop one pod by Runpod pod ID
/pod/resume -> resume one pod by Runpod pod ID
/pod/terminate -> terminate one pod by Runpod pod ID
/run/cleanup -> stop or terminate stale managed resources
/run/{run_id}/turn -> increment turn counter for keep-alive policy
```

Add basic input validation. Do not accept arbitrary shell commands from routes.

## 13. Environment Contract

All providers should normalize into a shared environment contract where possible.

### 13.1 Agent/session env

```text
RUNPOD_MANAGED_BY=comfyui-runpod-agentic
CRAG_RUN_ID=<run_id>
CRAG_WORKFLOW_HASH=<workflow_hash>
CRAG_NODE_ID=<node_id>
CRAG_ROLE=agent
WORKSPACE_DIR=/workspace
```

### 13.2 LLM env

```text
LLM_PROVIDER=codex|claude|ollama_cloud|ollama|vllm
LLM_API_FORMAT=openai|anthropic|ollama
LLM_MODEL=<model>
LLM_API_BASE_URL=<base_url>
```

Provider-specific aliases:

```text
OPENAI_API_KEY=<secret or generated token>
OPENAI_BASE_URL=<base_url>
OPENAI_MODEL=<model>

ANTHROPIC_API_KEY=<secret>
ANTHROPIC_MODEL=<model>

OLLAMA_API_KEY=<secret if cloud/private>
OLLAMA_HOST=<base_url>
OLLAMA_MODEL=<model>
```

### 13.3 Browser env

```text
BROWSER_KIND=playwright|neko
PLAYWRIGHT_MODE=local|remote
PLAYWRIGHT_WS_ENDPOINT=<url if remote>
NEKO_URL=<url if neko>
```

### 13.4 Database env

```text
DATABASE_KIND=postgres|mysql|sqlite
DATABASE_URL=<url>
```

### 13.5 Vector env

```text
VECTOR_KIND=chroma|qdrant
VECTOR_URL=<url>
VECTOR_COLLECTION=<collection_name>
```

### 13.6 S3 env

```text
S3_ENDPOINT=<endpoint>
S3_BUCKET=<bucket>
S3_REGION=<region>
AWS_ACCESS_KEY_ID={{ RUNPOD_SECRET_x }}
AWS_SECRET_ACCESS_KEY={{ RUNPOD_SECRET_y }}
```

## 14. Keep Alive Policy

### 14.1 Time policy

Convert to absolute `stopAfter` or `terminateAfter` timestamps where possible.

Also record the policy in SQLite for UI and reconciliation.

### 14.2 Turns policy

Requires runtime callback:

```text
POST /runpod-agentic/run/{run_id}/turn
```

The runtime supervisor, agent wrapper, or harness launch command should call this after each agent turn.

### 14.3 Cost policy

Estimate:

```text
estimated_cost = runtime_hours * cost_per_hr
```

Use the pod’s `costPerHr` or `adjustedCostPerHr` from Runpod API response where available.

If the estimate reaches the limit, apply the configured action:

```text
stop | terminate
```

## 15. Validation Rules

Implement `validation.py`.

Hard errors:

```text
- Neko with same_pod placement.
- Both llm_api and llm_server connected to Agent.
- Playwright same_pod but no matching agent template capability.
- LLM Server same_pod in MVP.
- SQLite path outside workspace unless explicitly allowed.
- Network storage requested without network_volume_id.
- S3 storage without secret refs.
- Run mode apply/apply_and_wait without RUNPOD_API_KEY.
```

Warnings:

```text
- SQLite without network storage means data may be ephemeral.
- Startup command attempts large installs; recommend template build instead.
- Cost keep-alive is estimated, not exact billing.
- Public HTTP ports expose services; prefer auth tokens for LLM/browser services.
```

## 16. Readiness Checks

Each own-pod app must define a readiness strategy.

```text
Agent primary pod:
  - SSH port available
  - optional supervisor /health endpoint

Playwright pod:
  - HTTP health endpoint if provided
  - websocket endpoint reachable if possible

Neko pod:
  - HTTP endpoint reachable

Ollama pod:
  - GET /api/tags or equivalent

vLLM pod:
  - GET /health

Postgres/MySQL:
  - command-level health check over SSH or app-specific port check

Chroma/Qdrant:
  - HTTP health endpoint or collection API check
```

The planner should use bounded retries and report clear failures.

## 17. Security Requirements

- Do not store literal API keys in ComfyUI workflow JSON.
- Do not print secrets in logs.
- Prefer Runpod secrets or server-side env vars.
- Public LLM/browser/database endpoints should have auth tokens where supported.
- Never expose arbitrary route-based shell execution from ComfyUI server routes.
- Redact values for env vars whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`.

## 18. Node Class Registration

In `__init__.py`:

```python
from .nodes import (
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
)

NODE_CLASS_MAPPINGS = {
    "RunpodAgent": RunpodAgentNode,
    "RunpodBrowser": RunpodBrowserNode,
    "RunpodLLMServer": RunpodLLMServerNode,
    "RunpodLLMApi": RunpodLLMApiNode,
    "RunpodSQLDatabase": RunpodSQLDatabaseNode,
    "RunpodVectorDatabase": RunpodVectorDatabaseNode,
    "RunpodNetworkStorage": RunpodNetworkStorageNode,
    "RunpodS3Storage": RunpodS3StorageNode,
    "RunpodSSHCommand": RunpodSSHCommandNode,
    "RunpodKeepAlive": RunpodKeepAliveNode,
    "RunpodPod": RunpodPodNode,
    "RunpodRun": RunpodRunNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RunpodAgent": "Agent",
    "RunpodBrowser": "Browser",
    "RunpodLLMServer": "LLM Server",
    "RunpodLLMApi": "LLM API",
    "RunpodSQLDatabase": "SQL Database",
    "RunpodVectorDatabase": "Vector Database",
    "RunpodNetworkStorage": "Network Storage",
    "RunpodS3Storage": "S3 Storage",
    "RunpodSSHCommand": "SSH Command",
    "RunpodKeepAlive": "Keep Alive",
    "RunpodPod": "Pod",
    "RunpodRun": "Runpod Run",
}
```

## 19. MVP Implementation Order

### Milestone 1 — Declarative nodes and plan mode

- Implement `types.py`, `specs.py`, all node classes.
- Implement `planner.py` enough to produce a readable plan.
- Implement `Runpod Run` in `plan` mode only.
- Add unit tests for spec construction and validation.

Acceptance:

```text
A ComfyUI workflow can connect:
Browser -> Agent -> Pod -> Run
SQL -> Agent
Vector -> Agent
LLM API -> Agent
SSH Command -> Pod
Keep Alive -> Pod
```

`Run` returns a JSON/text plan and does not call Runpod.

### Milestone 2 — Runpod client and state store

- Implement `runpod_client.py` with mocked tests.
- Implement `state_store.py`.
- Implement reconciliation skeleton.
- Implement server routes for listing resources.

Acceptance:

```text
Runpod API key is read server-side.
Mocked create/stop/resume/terminate tests pass.
State DB records runs/resources/events.
```

### Milestone 3 — Agent pod + SSH command sequencing

- Implement primary Agent pod creation/resume.
- Implement SSH endpoint extraction and command execution.
- Implement runtime config file writing.
- Implement harness launch placeholder.

Acceptance:

```text
Agent(OpenCode or chosen first harness)
+ SSH Command(before_start)
+ Pod
+ Run(apply)
```

creates a pod, waits for SSH, runs command, writes config, launches placeholder agent command, and records state.

### Milestone 4 — SQL and Vector dependencies

- Implement Postgres own-pod dependency.
- Implement SQLite file-only contract.
- Implement Qdrant own-pod dependency.
- Implement Chroma own-pod dependency.

Acceptance:

```text
SQL(Postgres) -> Agent -> Pod -> Run
Vector(Qdrant) -> Agent -> Pod -> Run
```

creates dependencies first and injects `DATABASE_URL` / `VECTOR_URL` into the agent runtime contract.

### Milestone 5 — Browser dependencies

- Implement Playwright same-pod validation.
- Implement Playwright own-pod endpoint injection.
- Add N.eko own-pod spec and template mapping.

Acceptance:

```text
Browser(Playwright same_pod) -> Agent -> Pod -> Run
Browser(Playwright own_pod) -> Agent -> Pod -> Run
```

both produce correct plans; at least one mode works end-to-end.

### Milestone 6 — LLM API and LLM Server

- Implement `LLM API` node for Codex/OpenAI, Claude/Anthropic, and Ollama Cloud.
- Implement `LLM Server` node for Ollama and vLLM.
- Implement LLM own-pod dependency planning and env injection.

Acceptance:

```text
LLM API(Claude) -> Agent -> Pod -> Run
LLM Server(vLLM) -> Agent -> Pod -> Run
LLM Server(Ollama) -> Agent -> Pod -> Run
```

produce valid runtime contracts; at least one external API and one self-hosted server path work end-to-end.

### Milestone 7 — Keep-alive and cleanup

- Implement time-based stop/terminate.
- Implement turn counter route.
- Implement estimated cost monitoring.
- Implement cleanup stale managed resources route.

Acceptance:

```text
Keep Alive(time=30min, action=stop) -> Pod -> Run
```

sets lifecycle policy and records it; cleanup route can stop/terminate stale managed pods.

## 20. Example Workflows

### 20.1 Agent with external Claude API, Postgres, Qdrant, and setup command

```text
LLM API(provider=Claude, model=<model>, secret=anthropic_key)
SQL Database(Postgres)
Vector Database(Qdrant)
Browser(Playwright, same_pod)
Agent(OpenCode, llm_api=..., sql_database=..., vector_database=..., browser=...)
SSH Command(phase=before_start, command="pip install -e /workspace/tools")
Network Storage(volume_id=..., mount=/workspace)
Keep Alive(time=30 minutes, action=stop)
Pod(app=Agent, network_storage=..., commands=..., keep_alive=...)
Run(mode=apply_and_wait)
```

Execution:

```text
1. Create Postgres pod.
2. Create Qdrant pod.
3. Wait for readiness.
4. Resolve DATABASE_URL and VECTOR_URL.
5. Create primary Agent pod with Playwright capability.
6. Run setup command over SSH.
7. Write runtime config.
8. Launch OpenCode harness.
9. Stop after keep-alive limit.
```

### 20.2 Agent with self-hosted vLLM

```text
LLM Server(vLLM, model=Qwen/Qwen3-0.6B)
Agent(Codex, llm_server=vLLM)
Pod(app=Agent)
Run(mode=apply)
```

Execution:

```text
1. Create vLLM pod.
2. Wait for /health.
3. Resolve OPENAI_BASE_URL.
4. Create Agent pod.
5. Launch Codex harness with OpenAI-compatible endpoint env.
```

### 20.3 Agent with SQLite and S3

```text
SQL Database(SQLite, path=/workspace/db/app.sqlite)
S3 Storage(bucket=..., secrets=...)
Agent(Pi, sql_database=SQLite)
Pod(app=Agent, s3_storage=S3)
Run(mode=apply)
```

Execution:

```text
1. No database pod is created.
2. Agent receives DATABASE_URL for SQLite.
3. Agent receives S3 env vars via secret refs.
4. Run creates only the primary agent pod.
```

## 21. Done Means Done

The MVP is complete when:

- All initial nodes appear in ComfyUI with correct categories and typed links.
- Non-Run nodes are pure/declarative.
- `Runpod Run(plan)` displays an accurate resource/action plan.
- `Runpod Run(apply)` can create an agent pod, run `before_start` SSH commands, write runtime config, and launch an agent command.
- Local SQLite records runs/resources/commands/events but Runpod remains the source of truth for real infrastructure state.
- Users can stop and terminate managed pods from server routes.
- Agent can accept optional browser, LLM API, LLM server, SQL database, and vector database inputs.
- Qdrant is included as the second vector database.
- Ollama and vLLM are included as self-hosted LLM server apps.
- Codex/OpenAI, Claude/Anthropic, and Ollama Cloud are included as LLM API providers.
