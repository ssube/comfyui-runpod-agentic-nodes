# Runpod Agentic Nodes User Guide

This package lets you design, launch, connect, monitor, and shut down agentic systems on Runpod from a ComfyUI graph.

The mission is to make infrastructure for agents visible and repeatable. A workflow should show the agent, its model, browser, databases, storage, skills, MCP servers, setup commands, lifetime policy, and run prompt as typed nodes. Most nodes only describe intent. The only node that creates, mutates, or cleans up real Runpod resources is `Runpod Run`.

## Core Mental Model

Build workflows in two layers:

1. Describe resources with typed nodes.
2. Execute the deployment with `Runpod Run`.

Resource nodes return Python spec objects. They do not call Runpod, start containers, SSH into pods, or write state. This makes the graph safe to edit and inspect. `Runpod Run` compiles the graph into a Terraform-style plan, reconciles it with existing managed resources, executes commands, writes runtime configuration, launches the agent, and applies cleanup behavior.

The most important graph shape is:

```text
LLM / Browser / DB / MCP / Skills
              -> Agent
              -> Pod
              -> Runpod Run
              -> PreviewAny or Runpod Logs
```

Use `Runpod Run` in `plan` mode first. A plan should explain what pods would be created or reused, what dependencies must become ready, what commands would run, what runtime files and environment contracts would be written, and what cleanup policy would apply.

## Resource Materialization

Every app-like node maps to one of these deployment styles:

| Materialization | Meaning | Examples |
| --- | --- | --- |
| `own_pod` | Create or reuse a dedicated Runpod pod. | Neko, remote Playwright, Ollama, vLLM, Postgres, MySQL, Qdrant, Chroma |
| `same_pod` | Enable the capability inside the primary agent pod. | Playwright |
| `file_only` | Provide a file path or config contract, no service process. | SQLite |
| `env_only` / `config_only` | Inject environment values, secrets, or config. | Codex/OpenAI API, Claude API, Ollama Cloud, S3, MCP config |

This matters because storage, readiness, cost, and cleanup differ. An `own_pod` service can have its own template and network storage. A `same_pod` feature depends on the agent image supporting that capability. An `env_only` API never starts a pod.

## Credentials And Secrets

The Runpod API token belongs in the ComfyUI server environment, not in workflow JSON:

```bash
export RUNPOD_API_KEY=...
```

The helper scripts also read `.env.d/runpod.env`, which is useful for local development when you cannot source environment variables into the tool process.

Node fields such as `api_key_secret_name`, `password_secret_name`, `hf_token_secret_name`, `access_key_secret_name`, and `secret_key_secret_name` are secret names, not raw secret values. They should refer to Runpod secrets or server-managed secret names that the runtime can resolve. Do not paste API keys, database passwords, or tokens into node widgets.

SSH access can also be configured through environment:

| Variable | Purpose |
| --- | --- |
| `RUNPOD_API_KEY` | Runpod GraphQL API token for apply/cleanup modes. |
| `RUNPOD_SSH_PRIVATE_KEY_PATH` | Default private key path for SSH operations. |
| `RUNPOD_SSH_PROXY_SUFFIX` | Runpod proxy SSH suffix when using proxy SSH. |
| `COMFYUI_USER_DIR` | Base directory for local state DB and collected logs outside ComfyUI. |

## Prompts

There are two prompts:

| Prompt | Node | Purpose |
| --- | --- | --- |
| `system_prompt` | `Runpod Agent` | Long-lived behavior and operating instructions for the agent. |
| `prompt` | `Runpod Run` | The specific task for this run. |

In the ComfyUI UI, prefer `PrimitiveStringMultiline` nodes connected to these string inputs. That keeps prompts readable in screenshots and makes the workflow easier to review. Do not create both `prompt` and `run_prompt`; the run prompt is simply `prompt`.

## Practical Workflow Recipe

1. Add any prompt primitives.
   Connect one multiline string to `Runpod Agent.system_prompt` and another to `Runpod Run.prompt` when you want visible prompt nodes.

2. Add LLM access.
   Use `Runpod LLM API` for hosted APIs or `Runpod LLM Server` for self-hosted Ollama/vLLM. Both output the generic `RUNPOD_LLM` type and connect to `Runpod Agent.llm`.

3. Add optional tools and state.
   Add `Browser`, `SQL Database`, `Vector Database`, `MCP Server`, `Skill`, or `Skill Framework` nodes, then connect them to the agent.

4. Add storage where persistence is needed.
   Connect `Network Storage` directly to the pod or to service nodes that run in their own pods. Service-specific storage is separate from the agent pod's workspace storage.

5. Add startup commands if needed.
   Chain `SSH Command` nodes through their `previous` input and connect the final output to `Runpod Pod.commands`.

6. Add lifecycle and access policy.
   Connect `Keep Alive` and, when command execution or direct access is needed, `SSH Access` to the pod.

7. Add `Runpod Pod`.
   Connect the agent to `app`, choose GPU hints, disk size, exposure, and reuse policy.

8. Add `Runpod Run`.
   Start with `mode=plan`. Inspect the JSON. Move to `apply`, `apply_and_wait`, `stop`, `terminate`, or `destroy` only after the plan is correct.

9. Inspect outputs and logs.
   Connect `Runpod Run.result` to `PreviewAny`. Use `Runpod Logs` with a run ID to collect saved command stdout/stderr.

10. Clean up.
   Use `terminate` or `destroy` when the deployment is no longer needed. For managed leftovers, use `scripts/cleanup-runpod-pods --action terminate`.

## Core Nodes

### Runpod Run

`Runpod Run` is the terminal execution node and the only side-effecting node.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `deployment` | `RUNPOD_DEPLOYMENT_SPEC` | The compiled pod deployment from `Runpod Pod`. |
| `mode` | `plan`, `apply`, `apply_and_wait`, `stop`, `terminate`, `destroy` | Selects whether to preview, create/run, wait, or clean up resources. |
| `prompt` | multiline string | The task prompt for this run. |
| `on_error` | `stop_created`, `terminate_created`, `leave_running` | Cleanup behavior if apply fails after resources were created. |
| `log_level` | `info`, `debug` | Verbosity for result JSON. |

Modes:

| Mode | Behavior |
| --- | --- |
| `plan` | Build and return a deployment plan. No Runpod calls. |
| `apply` | Create or resume resources, run setup, write runtime config, launch the agent. |
| `apply_and_wait` | Apply and wait for completion/readiness behavior supported by the runner. |
| `stop` | Stop managed pods for the deployment. |
| `terminate` | Terminate managed pods. |
| `destroy` | Clean managed resources and local state for the deployment. |

Output:

| Output | Type | Use |
| --- | --- | --- |
| `result` | `RUNPOD_RUN_RESULT` | JSON plan or execution result, usually connected to `PreviewAny`. |

### Runpod Pod

`Runpod Pod` wraps the primary agent and deployment policy.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `app` | `RUNPOD_APP_AGENT` | Required agent spec. |
| `gpu_type_id` | string | Runpod GPU type hint, for example `NVIDIA RTX A4000`. |
| `gpu_count` | integer | GPU count. `0` marks a CPU-only intent where supported by templates/API. |
| `cloud_type` | `auto`, `SECURE`, `COMMUNITY` | Runpod cloud selection. |
| `container_disk_gb` | integer | Container disk size. |
| `volume_gb` | integer | Pod volume size. |
| `expose_public_ip` | boolean | Whether public IP exposure is requested. |
| `reuse_policy` | `reuse_matching`, `always_create`, `resume_stopped` | How to reconcile with existing managed pods. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for the agent pod workspace. |
| `s3_storage` | `RUNPOD_STORAGE_S3` | Optional S3 env/config contract. |
| `commands` | `RUNPOD_COMMAND_SSH` | Optional command chain. |
| `keep_alive` | `RUNPOD_KEEPALIVE_POLICY` | Optional lifecycle policy. |
| `ssh_access` | `RUNPOD_SSH_ACCESS_POLICY` | Optional SSH connection policy. |

Output:

| Output | Type |
| --- | --- |
| `deployment` | `RUNPOD_DEPLOYMENT_SPEC` |

Use top-level `network_storage` for files the agent needs in `/workspace`. Use service-specific storage inputs on browser, LLM, SQL, or vector nodes when that service owns the persistent data.

### Runpod Keep Alive

`Runpod Keep Alive` describes when a created deployment should be stopped or terminated.

Inputs:

| Input | Choices | Use |
| --- | --- | --- |
| `mode` | `time`, `turns`, `cost`, `manual` | The condition being tracked. |
| `action` | `stop`, `terminate` | What to do when the condition is reached. |
| `time_value` | integer | Time quantity for `time` mode. |
| `time_unit` | `seconds`, `minutes`, `hours` | Unit for `time_value`. |
| `turn_limit` | integer | Turn count limit for `turns` mode. |
| `cost_limit_usd` | float | Cost threshold for `cost` mode. |
| `idle_grace_seconds` | integer | Grace period before lifecycle action. |

Output:

| Output | Type |
| --- | --- |
| `keep_alive` | `RUNPOD_KEEPALIVE_POLICY` |

For first real runs, prefer a short `time` policy with `action=terminate` until the workflow is stable. Use `stop` only when you intentionally want stopped pods to remain available.

### Runpod SSH Access

`Runpod SSH Access` defines how the runner reaches pods to execute commands and write runtime files.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `mode` | `runpod_proxy`, `internal_sshd` | Use Runpod proxy SSH or an internal SSH daemon. |
| `username` | string | SSH username, usually `root`. |
| `private_key_path` | string | Private key path. Can be overridden by env. |
| `proxy_key_suffix` | string | Runpod proxy user suffix. Can be overridden by env. |
| `internal_port` | integer | Internal SSH port. |
| `install_internal_sshd` | boolean | Inject startup setup to install/start sshd where supported. |

Output:

| Output | Type |
| --- | --- |
| `ssh_access` | `RUNPOD_SSH_ACCESS_POLICY` |

If a pod exposes an SSH port but rejects connections, the container may not be running an SSH server. Enable `install_internal_sshd` when using images that start as `sleep infinity` or otherwise lack sshd.

### Runpod Logs

`Runpod Logs` collects stdout and stderr captured from SSH command execution.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `run_id` | string | Run ID from a previous result. |
| `stream` | `both`, `stdout`, `stderr` | Which streams to collect. |
| `max_chars` | integer | Tail length for returned text. |
| `save_copy` | boolean | Save a copy under the ComfyUI user directory. |

Outputs:

| Output | Type | Use |
| --- | --- | --- |
| `logs` | `STRING` | Combined log text. |
| `saved_path` | `STRING` | Path to the saved copy, if enabled. |

## Agent And App Nodes

### Runpod Agent

`Runpod Agent` is the composition point for the runnable system.

Inputs:

| Input | Type / Choices | Use |
| --- | --- | --- |
| `harness` | `Codex`, `Claude`, `OpenCode`, `Hermes`, `Pi` | Agent runtime/harness. |
| `model` | string | Model name for the harness. |
| `startup_mode` | `wait_for_commands`, `auto_start`, `manual` | When the agent should start relative to setup. |
| `workspace_path` | string | Workspace path inside the agent pod. |
| `system_prompt` | multiline string | Long-lived system instructions. |
| `browser` | `RUNPOD_APP_BROWSER` | Optional browser capability. |
| `llm` | `RUNPOD_LLM` | Optional hosted API or self-hosted LLM server. |
| `sql_database` | `RUNPOD_APP_SQL_DATABASE` | Optional SQL contract. |
| `vector_database` | `RUNPOD_APP_VECTOR_DATABASE` | Optional vector DB contract. |
| `mcp_servers` | `RUNPOD_MCP_SERVERS` | Optional MCP server list. |
| `skills` | `RUNPOD_AGENT_SKILLS` | Optional downloaded skills/frameworks. |

Output:

| Output | Type |
| --- | --- |
| `agent` | `RUNPOD_APP_AGENT` |

Use `startup_mode=wait_for_commands` when setup commands, skills, MCP files, or runtime config must be prepared before the agent starts.

### Runpod Browser

`Runpod Browser` adds browser automation.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `browser` | `Neko`, `Playwright` | Browser implementation. |
| `placement` | `own_pod`, `same_pod` | Dedicated service pod or inside agent pod. |
| `browser_engine` | `chromium`, `firefox`, `chrome` | Browser engine selection. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for an own-pod browser. |

Output:

| Output | Type |
| --- | --- |
| `browser` | `RUNPOD_APP_BROWSER` |

`Neko` only supports `own_pod`. `Playwright` can run as `same_pod` when the agent image supports it, or as an `own_pod` remote browser service.

### Runpod LLM API

`Runpod LLM API` provides hosted model access through environment and secret contracts.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `provider` | `Codex`, `Claude`, `Ollama Cloud` | Hosted provider contract. |
| `model` | string | Model name. |
| `api_key_secret_name` | string | Secret name for the provider key. |
| `base_url_override` | string | Optional custom API base URL. |

Output:

| Output | Type |
| --- | --- |
| `llm` | `RUNPOD_LLM` |

Connect this to `Runpod Agent.llm`. It does not create a pod.

### Runpod LLM Server

`Runpod LLM Server` creates a self-hosted LLM service pod.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `engine` | `Ollama`, `vLLM` | Serving engine. |
| `model` | string | Model to serve. |
| `placement` | `own_pod` | LLM servers are own-pod only in the current MVP. |
| `api_auth_mode` | `none`, `generated_token`, `secret` | Auth contract for the service endpoint. |
| `api_key_secret_name` | string | Secret name when `api_auth_mode=secret`. |
| `hf_token_secret_name` | string | Optional Hugging Face token secret. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for model caches/data. |

Output:

| Output | Type |
| --- | --- |
| `llm` | `RUNPOD_LLM` |

Use Ollama for Ollama-compatible workflows and vLLM for OpenAI-compatible serving. The planner creates the LLM pod before the agent and injects endpoint variables into the agent runtime contract.

## Data Nodes

### Runpod SQL Database

`Runpod SQL Database` provides SQL state.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `engine` | `Postgres`, `MySQL`, `SQLite` | Database engine. |
| `database_name` | string | Database name for server engines. |
| `username` | string | Database username for server engines. |
| `password_secret_name` | string | Secret name for the DB password. |
| `sqlite_path` | string | SQLite DB path when `engine=SQLite`. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for own-pod DB engines. |

Output:

| Output | Type |
| --- | --- |
| `sql_database` | `RUNPOD_APP_SQL_DATABASE` |

Postgres and MySQL create service pods. SQLite is `file_only`, so no database pod is created; put the SQLite path under the agent workspace if it must persist with workspace storage.

### Runpod Vector Database

`Runpod Vector Database` provides a retrieval store.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `engine` | `Chroma`, `Qdrant` | Vector DB engine. |
| `collection_name` | string | Collection/index name. |
| `persistence_path` | string | Persistence path inside the vector service. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for vector data. |

Output:

| Output | Type |
| --- | --- |
| `vector_database` | `RUNPOD_APP_VECTOR_DATABASE` |

Qdrant uses an HTTP service contract on port 6333. Chroma uses an HTTP service contract on port 8000.

## Skills And MCP Nodes

### Runpod MCP Server

`Runpod MCP Server` defines one or more Model Context Protocol servers for the agent.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `name` | string | MCP server name in the generated config. |
| `transport` | `stdio`, `http`, `sse` | MCP transport. |
| `command` | string | Command for `stdio` servers. |
| `args` | string | Shell-style args for `stdio` servers. |
| `url` | string | URL for `http` or `sse` servers. |
| `env_json` | multiline JSON object | Static environment values for the server. |
| `secret_env_names` | comma-separated string | Env names backed by secrets. |
| `previous` | `RUNPOD_MCP_SERVERS` | Optional previous MCP chain. |

Output:

| Output | Type |
| --- | --- |
| `mcp_servers` | `RUNPOD_MCP_SERVERS` |

The node is chainable. Connect the previous MCP output into the next node's `previous` input, then connect the final output to `Runpod Agent.mcp_servers`.

### Runpod Skill

`Runpod Skill` downloads a skill from a GitHub repository into the agent working state.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `name` | string | Skill name. |
| `github_repo_url` | string | GitHub repo URL. Must start with `https://github.com/` or `git@github.com:`. |
| `repo_path` | string | Path inside the repo to copy. |
| `target_path` | string | Destination in the pod. Defaults to `/workspace/.codex/skills/{name}`. |
| `git_ref` | string | Optional branch, tag, or commit. |
| `previous` | `RUNPOD_AGENT_SKILLS` | Optional previous skills chain. |

Output:

| Output | Type |
| --- | --- |
| `skills` | `RUNPOD_AGENT_SKILLS` |

The planner adds startup commands to clone the repo and copy the requested path into the agent workspace before launch.

### Runpod Skill Framework

`Runpod Skill Framework` installs a known skill framework or a custom framework repo.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `framework` | `Superpowers`, `Superpowers Skills`, `Anthropic Skills`, `Custom GitHub Repo` | Framework preset. |
| `custom_github_repo_url` | string | Repo URL when using `Custom GitHub Repo`. |
| `custom_repo_path` | string | Repo path when using `Custom GitHub Repo`. |
| `target_root` | string | Destination root, default `/workspace/.codex/skills`. |
| `git_ref` | string | Optional branch, tag, or commit. |
| `previous` | `RUNPOD_AGENT_SKILLS` | Optional previous skills chain. |

Output:

| Output | Type |
| --- | --- |
| `skills` | `RUNPOD_AGENT_SKILLS` |

Use this when you want a curated framework such as Superpowers without hand-entering the repository URL and path.

## Storage And Command Nodes

### Runpod Network Storage

`Runpod Network Storage` attaches an existing Runpod network volume.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `network_volume_id` | string | Existing Runpod network volume ID. |
| `mount_path` | string | Container mount path. |

Output:

| Output | Type |
| --- | --- |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` |

Where you connect the node determines where the volume is mounted:

| Connected to | Effect |
| --- | --- |
| `Runpod Pod.network_storage` | Agent pod workspace/storage. |
| `Runpod Browser.network_storage` | Browser service pod storage. |
| `Runpod LLM Server.network_storage` | LLM service pod storage. |
| `Runpod SQL Database.network_storage` | SQL service pod storage. |
| `Runpod Vector Database.network_storage` | Vector service pod storage. |

### Runpod S3 Storage

`Runpod S3 Storage` injects S3-compatible storage configuration.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `endpoint` | string | S3-compatible endpoint. |
| `bucket` | string | Bucket name. |
| `region` | string | Region. |
| `access_key_secret_name` | string | Secret name for access key. |
| `secret_key_secret_name` | string | Secret name for secret key. |

Output:

| Output | Type |
| --- | --- |
| `s3_storage` | `RUNPOD_STORAGE_S3` |

Connect this to `Runpod Pod.s3_storage`. The node injects S3 environment variables and secret references; it does not create a bucket.

### Runpod SSH Command

`Runpod SSH Command` adds setup or teardown commands.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `command` | multiline string | Command body. |
| `phase` | `before_start`, `after_start`, `after_ready`, `teardown` | Execution phase. |
| `order` | integer | Sort key within the command chain. |
| `failure_policy` | `fail`, `continue`, `retry` | Error behavior. |
| `retry_count` | integer | Retry count when `failure_policy=retry`. |
| `previous` | `RUNPOD_COMMAND_SSH` | Optional previous command chain. |

Output:

| Output | Type |
| --- | --- |
| `commands` | `RUNPOD_COMMAND_SSH` |

The node is chainable. Connect the final command chain to `Runpod Pod.commands`. Use `before_start` for dependency installation and workspace setup. Use `after_ready` for checks that need services to be listening.

## Common Workflow Patterns

### Plan-Only Minimal Agent

Use this to validate templates and graph structure without creating pods:

```text
PrimitiveStringMultiline(system prompt)
PrimitiveStringMultiline(task prompt)
Runpod LLM API(provider=Claude or Codex)
Runpod Agent(llm=LLM API, system_prompt=system prompt)
Runpod Pod(app=Agent, reuse_policy=reuse_matching)
Runpod Run(mode=plan, prompt=task prompt)
PreviewAny(source=Runpod Run.result)
```

### Agent With Hosted LLM, Browser, And Skills

```text
Runpod LLM API -> Agent.llm
Runpod Browser(Playwright, same_pod) -> Agent.browser
Runpod Skill Framework(Superpowers) -> Runpod Skill(previous=framework) -> Agent.skills
Agent -> Pod -> Runpod Run
```

This avoids a self-hosted model pod and keeps the graph focused on agent workspace setup.

### Agent With Self-Hosted Ollama Or vLLM

```text
Network Storage(model-cache) -> LLM Server.network_storage
Runpod LLM Server(engine=Ollama or vLLM, placement=own_pod) -> Agent.llm
Agent -> Pod -> Runpod Run
```

The LLM server starts before the agent. Use `hf_token_secret_name` for private Hugging Face models with vLLM.

### Stateful Research Agent

```text
Network Storage(postgres-data) -> SQL Database.network_storage
Network Storage(qdrant-data) -> Vector Database.network_storage
Runpod SQL Database(Postgres) -> Agent.sql_database
Runpod Vector Database(Qdrant) -> Agent.vector_database
Runpod Browser(Playwright, same_pod) -> Agent.browser
Runpod LLM API(Claude) -> Agent.llm
Agent -> Pod -> Runpod Run
```

This pattern is useful when the agent needs both structured state and retrieval state.

### Command-Driven Setup

```text
SSH Command(order=10, phase=before_start, command="python -m pip install -r requirements.txt")
SSH Command(previous=first, order=20, phase=before_start, command="python scripts/bootstrap.py")
final commands -> Pod.commands
```

Keep commands idempotent when using `reuse_matching` or `resume_stopped`, because they may run against an existing workspace.

## Example Workflows

UI-format examples for loading into ComfyUI:

| File | Purpose |
| --- | --- |
| `examples/workflows/ui_agent_skills_mcp_plan.json` | Agent with MCP servers and skills. |
| `examples/workflows/ui_claude_data_agent_plan.json` | Claude API, Playwright, Postgres, Qdrant, and prompts. |
| `examples/workflows/ui_neko_ollama_agent_plan.json` | Neko browser and self-hosted Ollama. |

API-format examples:

| File | Purpose |
| --- | --- |
| `examples/workflows/api_plan_smoke.json` | Small API prompt for plan-mode smoke tests. |
| `examples/workflows/api_real_neko_ollama_apply.json` | Real Runpod apply workflow for Neko plus Ollama. |

Generate workflow screenshots:

```bash
scripts/screenshot-ui-workflows --skip-clone
```

Submit an API workflow to a running ComfyUI server:

```bash
scripts/submit-comfy-api-workflow --workflow examples/workflows/api_plan_smoke.json
```

## Testing And Live Operations

Offline tests do not require Runpod credentials:

```bash
scripts/test
scripts/lint
```

Verify the nodes load in a temporary CPU-only ComfyUI server:

```bash
scripts/e2e-comfy-cpu
```

Create or update Runpod templates and persist their IDs:

```bash
scripts/create-runpod-templates \
  --spec defaults/runpod_templates.bootstrap.json \
  --map defaults/runpod_template_ids.json
```

Run a minimal live smoke test:

```bash
scripts/run-live-smoke \
  --cloud-type COMMUNITY \
  --gpu-type-id "NVIDIA GeForce RTX 3090"
```

Clean up managed pods:

```bash
scripts/cleanup-runpod-pods --action terminate
```

## Troubleshooting

Plan succeeds but apply fails with missing credentials:

Set `RUNPOD_API_KEY` in the ComfyUI server environment or `.env.d/runpod.env` for scripts. `plan` mode does not need a token; apply and cleanup modes do.

SSH port is open but connections are refused:

The container may not be running an SSH daemon. Add `Runpod SSH Access` with `install_internal_sshd=true`, or use an image/template that starts sshd itself.

`sshd: no hostkeys available -- exiting`:

The image attempted to start sshd before host keys existed. Use the internal sshd setup path or ensure your template generates host keys before starting sshd.

Neko cannot use `same_pod`:

Use `placement=own_pod` for Neko. Use Playwright when you need `same_pod` browser automation.

LLM Server cannot use `same_pod`:

The current MVP supports `own_pod` only for Ollama and vLLM. Use `Runpod LLM API` for env-only hosted access.

Stopped pods are left behind:

Use `terminate_created` in `Runpod Run.on_error`, set keep-alive action to `terminate` for tests, or run `scripts/cleanup-runpod-pods --action terminate`.

Logs are missing:

`Runpod Logs` reads command logs captured by the local state store. It will not show provider/container logs unless those logs were captured by commands or runner state.

## Design Checklist

Before applying a real workflow, verify:

- The graph uses `Runpod Run(mode=plan)` successfully.
- No raw credentials appear in node widgets or JSON.
- The agent has exactly one intended LLM connection on `llm`.
- Long prompts are visible through multiline primitive nodes.
- Any persistent service has the correct `Network Storage` input connected.
- `SSH Access` matches how the pod can actually be reached.
- `on_error` and `Keep Alive` will not leave unwanted stopped pods.
- The result is connected to `PreviewAny` or logs can be collected with `Runpod Logs`.
