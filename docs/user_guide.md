# ComfyUI Runpod Agentic Workflow Nodes User Guide

ComfyUI Runpod Agentic workflow nodes, or CRAG nodes, let you design, launch, connect, monitor, and shut down agentic systems on Runpod from a ComfyUI graph.

The CRAG node mission is to make infrastructure for agents visible and repeatable. A workflow should show the agent, its model, browser, databases, storage, skills, MCP servers, setup commands, lifetime policy, and run prompt as typed nodes. Most nodes only describe intent. The only node that creates, mutates, or cleans up real Runpod resources is `Run on Runpod`.

## Core Mental Model

Build workflows in two layers:

1. Describe resources with typed nodes.
2. Execute the deployment with `Run on Runpod`.

CRAG resource nodes return Python spec objects. They do not call Runpod, start containers, SSH into pods, or write state. This makes the graph safe to edit and inspect. `Run on Runpod` compiles the graph into a Terraform-style plan, reconciles it with existing managed resources, executes commands, writes runtime configuration, launches the agent, and applies cleanup behavior.

The low-level contract is intentionally small:

| Effect | Contract Surface | Examples |
| --- | --- | --- |
| Create or reuse a Runpod resource and pass env forward | `ResourcePlan` plus `RuntimeContract.env`, `ports`, and storage hints | Browser pod, LLM server pod, remote SQL pod, vector DB pod |
| Queue a command to run after pods launch | `RuntimeContract.commands` | Local SQLite setup, skill download, framework install, user SSH commands |

Higher-level nodes should compose these effects rather than calling providers themselves. A future node should either describe a pod-like resource that `Run on Runpod` can materialize, contribute environment/runtime files to the later agent pod, or add idempotent SSH commands to the runtime contract.

The most important graph shape is:

```text
LLM / Browser / DB / MCP / Skills
              -> Agent
              -> Runpod Pod
              -> Run on Runpod
              -> PreviewAny or Runpod Logs
```

Use `Run on Runpod` in `plan` mode first. A plan should explain what pods would be created or reused, what dependencies must become ready, what commands would run, what runtime files and environment contracts would be written, and what cleanup policy would apply.

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
| `prompt` | `Run on Runpod` | The specific task for this run. |

In the ComfyUI UI, prefer `PrimitiveStringMultiline` nodes connected to these string inputs. That keeps prompts readable in screenshots and makes the workflow easier to review. Do not create both `prompt` and `run_prompt`; the run prompt is simply `prompt`.

## Practical Workflow Recipe

1. Add any prompt primitives.
   Connect one multiline string to `Runpod Agent.system_prompt` and another to `Run on Runpod.prompt` when you want visible prompt nodes.

2. Add LLM access.
   Use `Runpod LLM API` for hosted APIs or `Runpod LLM Server` for self-hosted Ollama/vLLM. Both output the generic `RUNPOD_LLM` type and connect to `Runpod Agent.llm`.

3. Add optional tools and state.
   Add `Browser`, `Remote SQL Database`, `Local SQL Database`, `Vector Database`, `MCP Server`, `Skill`, or `Skill Framework` nodes, then connect them to the agent.

4. Add storage where persistence is needed.
   Connect `Network Storage` directly to the pod or to service nodes that run in their own pods. Service-specific storage is separate from the agent pod's workspace storage.

5. Add startup commands if needed.
   Chain `SSH Command` nodes through their `previous` input and connect the final output to `Runpod Pod.commands`.

6. Add lifecycle and access policy.
   Connect `Keep Alive` and, when command execution or direct access is needed, `SSH Access` to the pod.

7. Add `Runpod Pod`.
   Connect the agent to `app`, choose GPU hints, disk size, exposure, and reuse policy.

8. Add `Run on Runpod`.
   Start with `mode=plan`. Inspect the JSON. Move to `apply`, `apply_and_wait`, `stop`, `terminate`, or `destroy` only after the plan is correct.

9. Inspect outputs and logs.
   Connect `Run on Runpod.result` to `PreviewAny`. Use `Runpod Logs` with a run ID to collect saved command stdout/stderr.

10. Clean up.
   Use `terminate` or `destroy` when the deployment is no longer needed. For managed leftovers, use `scripts/cleanup-runpod-pods --action terminate`.

## Core Nodes

### Run on Runpod

`Run on Runpod` is the terminal execution node and the only side-effecting node.

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

### Runpod Startup Script

`Runpod Startup Script` exports the agent pod startup sequence as a single pasteable bash command. It does not call Runpod.

Inputs:

| Input | Type | Use |
| --- | --- | --- |
| `deployment` | `RUNPOD_DEPLOYMENT_SPEC` | The compiled pod deployment from `Runpod Pod`. |
| `prompt` | multiline string | Task prompt to write into the generated runtime files. |

Output:

| Output | Type | Use |
| --- | --- | --- |
| `startup_script` | `STRING` | Pasteable bash command, usually connected to `PreviewAny`. |

Use this when you want to inspect or manually run the exact `.runpod_agentic` bootstrap that CRAG would inject over SSH.

### Local Runtime Nodes

Local runtime nodes project the same deployment graph into a Compose YAML file so you can test the topology on your workstation before spending Runpod GPU time. This is intentionally close to the Runpod model: each own-pod resource becomes a service, dependency links are passed through environment variables, and network storage becomes named volumes with retention-policy labels.

| Node | Purpose |
| --- | --- |
| `Compose YAML` | Builds and optionally saves the Compose YAML without applying it. |
| `Docker Compose Apply` | Saves the YAML, then runs `docker compose`. |
| `Podman Compose Apply` | Saves the YAML, then runs `podman compose` or `podman-compose`. |
| `Containerd Apply` | Saves the YAML, then runs `nerdctl compose` for containerd-backed local testing. |

Inputs shared by the apply nodes:

| Input | Type | Use |
| --- | --- | --- |
| `deployment` | `RUNPOD_DEPLOYMENT_SPEC` | The compiled deployment from `Runpod Pod`. |
| `prompt` | multiline string | Task prompt injected as `AGENT_PROMPT`. |
| `project_name` | string | Compose project name and container-name prefix. |
| `output_path` | string | File path where the generated YAML is saved. |
| `action` | `save_only`, `config`, `pull`, `up`, `down` | Runtime action. Use `save_only` or `config` first when inspecting a new graph. |
| `timeout_seconds` | integer | Timeout for the local runtime command. |

Outputs:

| Output | Type | Use |
| --- | --- | --- |
| `result` | `STRING` | JSON summary with command, return code, stdout, and stderr. |
| `compose_yaml` | `STRING` | Generated Compose YAML, useful with `PreviewAny`. |
| `saved_path` | `STRING` | Path to the saved YAML file. |

`Containerd Apply` uses `nerdctl compose` rather than raw `ctr`; direct `ctr` does not provide the Compose-level dependency, env, port, and volume model these workflows need. If `nerdctl` is not installed, the node reports that as an apply error and still leaves the YAML on disk.

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

### Injected Runtime Launcher

`Run on Runpod` writes a common launcher runtime into the agent workspace before launch:

| Path | Purpose |
| --- | --- |
| `.runpod_agentic/launcher.sh` | Stable entrypoint used by the runner. |
| `.runpod_agentic/launcher.d/*.sh` | Environment and preflight hook scripts loaded before dispatch. |
| `.runpod_agentic/launcher.d/pre.d/*.sh` | Optional user-provided pre-launch hooks. |
| `.runpod_agentic/launcher.d/harnesses/*.sh` | Per-harness stubs for Codex, Claude, OpenCode, and generic fallback behavior. |

This makes CRAG usable with arbitrary SSH-capable containers. The container needs the requested agent CLI installed, or a `CRAG_AGENT_LAUNCH_COMMAND` override, but it does not need to include CRAG-specific files in the image. Advanced users can add or replace harness scripts with startup commands before launch.

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

### Runpod Remote SQL Database

`Runpod Remote SQL Database` provides server-style SQL state through Postgres or MySQL.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `engine` | `Postgres`, `MySQL` | Remote database engine. |
| `connection_mode` | `own_pod`, `env_only` | Create a DB pod or inject an existing connection from server env. |
| `database_name` | string | Database name. |
| `username` | string | Database username. |
| `password_secret_name` | string | Runpod secret name for own-pod DB password. |
| `database_url_env_var` | string | Server env var to map into pod `DATABASE_URL` when `connection_mode=env_only`. |
| `network_storage` | `RUNPOD_STORAGE_NETWORK` | Optional storage for own-pod DB engines. |

Output:

| Output | Type |
| --- | --- |
| `sql_database` | `RUNPOD_APP_SQL_DATABASE` |

Use `own_pod` when CRAG should create a managed Postgres/MySQL pod. Use `env_only` when the database already exists and the ComfyUI server has a connection string such as `APP_DATABASE_URL`; the runner injects that value into the agent pod as `DATABASE_URL`.

### Runpod Local SQL Database

`Runpod Local SQL Database` provides file-backed SQLite state in the agent workspace.

Inputs:

| Input | Choices / Type | Use |
| --- | --- | --- |
| `engine` | `SQLite` | Local database engine. |
| `database_name` | string | Logical database name. |
| `database_path` | string | SQLite DB path inside the agent pod. |

Output:

| Output | Type |
| --- | --- |
| `sql_database` | `RUNPOD_APP_SQL_DATABASE` |

SQLite is `file_only`, so no database pod is created. The planner queues a `before_start` setup command that installs `sqlite3` when needed, creates the containing directory, touches the DB file, and verifies the file with `PRAGMA user_version;`. Keep the path under the agent workspace if it must persist with workspace storage.

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
| `retention_policy` | `preserve`, `delete_when_unused`, `delete_with_deployment` | Declared lifecycle intent for the volume. Defaults to `preserve`. |

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
| `Runpod Remote SQL Database.network_storage` | SQL service pod storage. |
| `Runpod Vector Database.network_storage` | Vector service pod storage. |

Use `retention_policy=preserve` for important data. Destructive retention policies are surfaced as plan warnings so users can notice volume deletion intent before running lifecycle modes.

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
Run on Runpod(mode=plan, prompt=task prompt)
PreviewAny(source=Run on Runpod.result)
```

### Agent With Hosted LLM, Browser, And Skills

```text
Runpod LLM API -> Agent.llm
Runpod Browser(Playwright, same_pod) -> Agent.browser
Runpod Skill Framework(Superpowers) -> Runpod Skill(previous=framework) -> Agent.skills
Agent -> Runpod Pod -> Run on Runpod
```

This avoids a self-hosted model pod and keeps the graph focused on agent workspace setup.

### Agent With Self-Hosted Ollama Or vLLM

```text
Network Storage(model-cache) -> LLM Server.network_storage
Runpod LLM Server(engine=Ollama or vLLM, placement=own_pod) -> Agent.llm
Agent -> Runpod Pod -> Run on Runpod
```

The LLM server starts before the agent. Use `hf_token_secret_name` for private Hugging Face models with vLLM.

### Stateful Research Agent

```text
Network Storage(postgres-data) -> Remote SQL Database.network_storage
Network Storage(qdrant-data) -> Vector Database.network_storage
Runpod Remote SQL Database(Postgres) -> Agent.sql_database
Runpod Vector Database(Qdrant) -> Agent.vector_database
Runpod Browser(Playwright, same_pod) -> Agent.browser
Runpod LLM API(Claude) -> Agent.llm
Agent -> Runpod Pod -> Run on Runpod
```

This pattern is useful when the agent needs both structured state and retrieval state.

### Command-Driven Setup

```text
SSH Command(order=10, phase=before_start, command="python -m pip install -r requirements.txt")
SSH Command(previous=first, order=20, phase=before_start, command="python scripts/bootstrap.py")
final commands -> Runpod Pod.commands
```

Keep commands idempotent when using `reuse_matching` or `resume_stopped`, because they may run against an existing workspace.

### Local Container Rehearsal

```text
Agent -> Runpod Pod -> Compose YAML -> PreviewAny
Agent -> Runpod Pod -> Docker Compose Apply(action=config or up)
Agent -> Runpod Pod -> Podman Compose Apply(action=config or up)
Agent -> Runpod Pod -> Containerd Apply(action=config or up)
```

Use local rehearsal to verify service wiring, environment variables, ports, and startup commands. Treat it as a topology test, not a perfect Runpod emulator: GPU scheduling, Runpod secrets, public proxy ports, and Runpod lifecycle policies still need live Runpod validation.

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

Verify the CRAG nodes load in a temporary CPU-only ComfyUI server:

```bash
scripts/e2e-comfy-cpu
```

Create or update Runpod templates and persist their IDs:

```bash
scripts/create-runpod-templates \
  --spec defaults/runpod_templates.bootstrap.json \
  --map defaults/runpod_template_ids.json
```

Validate the live Runpod GraphQL schema before changing pod/template API code:

```bash
scripts/check-runpod-schema --json
```

Run a minimal live smoke test:

```bash
scripts/run-live-smoke \
  --cloud-type COMMUNITY \
  --gpu-type-id "NVIDIA GeForce RTX 3090"
```

Run opt-in live pytest checks:

```bash
RUNPOD_LIVE_TESTS=1 scripts/test tests/test_runpod_live.py
```

The live pod creation test is skipped unless `RUNPOD_LIVE_CREATE_POD=1`, `RUNPOD_TEST_TEMPLATE_ID`, and `RUNPOD_TEST_GPU_TYPE_ID` are also set.

Clean up managed pods:

```bash
scripts/cleanup-runpod-pods --action terminate
```

## Troubleshooting

Plan succeeds but apply fails with missing credentials:

Set `RUNPOD_API_KEY` in the ComfyUI server environment or `.env.d/runpod.env` for scripts. `plan` mode does not need a token; apply and cleanup modes do.

SSH port is open but connections are refused:

The container may not be running an SSH daemon. Add `Runpod SSH Access` with `install_internal_sshd=true`, or use an image/template that starts sshd itself.

Agent launch fails with exit code 127:

The injected launcher could not find a compatible agent CLI. Install the requested CLI in the container, include `runpod-agent-launch` on `PATH`, add a harness stub under `.runpod_agentic/launcher.d/harnesses/`, or set `CRAG_AGENT_LAUNCH_COMMAND` in the ComfyUI server environment to the exact startup command. The runner writes the CRAG runtime layer over SSH before launch, so the shim itself does not need to be baked into each image.

`sshd: no hostkeys available -- exiting`:

The image attempted to start sshd before host keys existed. Use the internal sshd setup path or ensure your template generates host keys before starting sshd.

Neko cannot use `same_pod`:

Use `placement=own_pod` for Neko. Use Playwright when you need `same_pod` browser automation.

LLM Server cannot use `same_pod`:

The current MVP supports `own_pod` only for Ollama and vLLM. Use `Runpod LLM API` for env-only hosted access.

Stopped pods are left behind:

Use `terminate_created` in `Run on Runpod.on_error`, set keep-alive action to `terminate` for tests, or run `scripts/cleanup-runpod-pods --action terminate`.

Logs are missing:

`Runpod Logs` reads command logs captured by the local state store. It will not show provider/container logs unless those logs were captured by commands or runner state.

## Design Checklist

Before applying a real workflow, verify:

- The graph uses `Run on Runpod(mode=plan)` successfully.
- No raw credentials appear in node widgets or JSON.
- The agent has exactly one intended LLM connection on `llm`.
- Long prompts are visible through multiline primitive nodes.
- Any persistent service has the correct `Network Storage` input connected.
- `SSH Access` matches how the pod can actually be reached.
- `on_error` and `Keep Alive` will not leave unwanted stopped pods.
- The result is connected to `PreviewAny` or logs can be collected with `Runpod Logs`.
