import json

from comfyui_runpod_agentic.nodes import (
    AgentNode,
    BrowserNode,
    DeployNode,
    KeepAliveNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    NetworkStorageNode,
    RemoteSQLDatabaseNode,
    SSHAccessNode,
    SSHCommandNode,
    VectorDatabaseNode,
)
from comfyui_runpod_agentic.planner import Planner


def build_deployment():
    llm = LLMApiNode().build("Claude", "claude-sonnet", "anthropic_key")[0]
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app", "pg_password")[0]
    vector = VectorDatabaseNode().build("Qdrant", "docs")[0]
    browser = BrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = AgentNode().build("OpenCode", "claude-sonnet", "wait_for_commands", browser=browser, llm=llm, sql_database=sql, vector_database=vector, node_id="agent1")[0]
    commands = SSHCommandNode().build("echo setup", "before_start", 10, "fail")[0]
    keep_alive = KeepAliveNode().build("time", "stop", 30, "minutes", 0, 0.0, 0)[0]
    return DeployNode().build(agent, gpu_type_id="NVIDIA A40", commands=commands, keep_alive=keep_alive)[0]


def test_planner_orders_dependencies_before_agent():
    plan = Planner().build(build_deployment(), workflow_graph={"id": "prompt"})
    actions = [action.action for action in plan.actions]

    assert actions.index("RESOLVE_DEPENDENCY_CONTRACTS") < actions.index("CREATE_OR_RESUME", 2)
    assert [resource.role for resource in plan.resources] == ["sql", "vector", "agent"]
    assert plan.resources[-1].template_id == "rp-agent-opencode-playwright"


def test_plan_is_json_serializable():
    plan = Planner().build(build_deployment(), prompt="Do the thing.")

    encoded = json.dumps(plan.to_dict(), sort_keys=True)

    assert "WRITE_RUNTIME_CONFIG" in encoded
    assert "ANTHROPIC_API_KEY" in encoded
    assert plan.prompt == "Do the thing."
    assert plan.runtime_contract.env.values["AGENT_PROMPT"] == "Do the thing."


def test_keep_alive_enforcement_controls_runpod_server_fields():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    server_policy = KeepAliveNode().build("time", "stop", 30, "seconds", 0, 0.0, 0, "server_side")[0]
    pod_policy = KeepAliveNode().build("time", "stop", 30, "seconds", 0, 0.0, 0, "pod_side")[0]

    server_plan = Planner().build(DeployNode().build(agent, gpu_count=0, keep_alive=server_policy)[0])
    pod_plan = Planner().build(DeployNode().build(agent, gpu_count=0, keep_alive=pod_policy)[0])

    assert "stopAfter" in server_plan.resources[-1].pod_input
    assert "stopAfter" not in pod_plan.resources[-1].pod_input


def test_ollama_dependency_binds_to_local_interface_but_agent_gets_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("RUNPOD_SSH_PRIVATE_KEY_PATH", raising=False)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("private")
    key_path.with_suffix(".pub").write_text("ssh-ed25519 test-key")
    browser = BrowserNode().build("Neko", "own_pod", "chromium")[0]
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = AgentNode().build("Pi", "model", "manual", browser=browser, llm=llm)[0]
    ssh_access = SSHAccessNode().build("runpod_proxy", "root", str(key_path), "suffix", 22, True)[0]
    deployment = DeployNode().build(agent, gpu_count=0, ssh_access=ssh_access)[0]

    plan = Planner().build(deployment)

    llm_resource = next(resource for resource in plan.resources if resource.role == "llm")
    agent_resource = next(resource for resource in plan.resources if resource.role == "agent")
    assert llm_resource.pod_input["env"]["OLLAMA_HOST"] == "0.0.0.0:11434"
    assert any(port["container_port"] == 22 for port in llm_resource.pod_input["ports"])
    assert agent_resource.pod_input["ports"] == [{"name": "ssh", "container_port": 22, "protocol": "tcp", "public": True}]
    assert agent_resource.pod_input["env"]["RUNPOD_SSH_PUBLIC_KEY"] == "ssh-ed25519 test-key"
    assert "base64 -d" in agent_resource.pod_input["dockerArgs"]
    assert "/tmp/runpod-agentic-sshd.sh" in agent_resource.pod_input["dockerArgs"]
    assert plan.runtime_contract.env.values["OLLAMA_HOST"] == "crag://llm/ollama"


def test_dependency_pods_use_their_own_network_storage():
    storage = NetworkStorageNode().build("vol-sql", "/var/lib/postgresql/data")[0]
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app", "pg_password", network_storage=storage)[0]
    agent = AgentNode().build("Pi", "model", "manual", sql_database=sql)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]

    plan = Planner().build(deployment)

    sql_resource = next(resource for resource in plan.resources if resource.role == "sql")
    agent_resource = next(resource for resource in plan.resources if resource.role == "agent")
    assert sql_resource.pod_input["networkVolumeId"] == "vol-sql"
    assert sql_resource.pod_input["volumeMountPath"] == "/var/lib/postgresql/data"
    assert "networkVolumeId" not in agent_resource.pod_input


def test_network_storage_retention_policy_is_visible_in_plan_warnings():
    storage = NetworkStorageNode().build("vol-workspace", "/workspace", "delete_when_unused")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0, network_storage=storage)[0]

    plan = Planner().build(deployment)

    assert deployment.network_storage.retention_policy == "delete_when_unused"
    assert any("retention_policy=delete_when_unused" in warning for warning in plan.warnings)


def test_local_sql_adds_sqlite_setup_before_user_commands():
    sql = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", sql_database=sql)[0]
    commands = SSHCommandNode().build("echo user", "before_start", 10, "fail")[0]
    deployment = DeployNode().build(agent, gpu_count=0, commands=commands)[0]

    plan = Planner().build(deployment)

    setup = [action for action in plan.actions if action.action == "RUN_SSH_COMMAND" and "sqlite3" in action.detail["command"]]
    assert setup
    assert setup[0].detail["order"] == -20000
    assert setup[0].detail["phase"] == "before_start"


def test_remote_env_sql_does_not_create_dependency_pod():
    sql = RemoteSQLDatabaseNode().build("Postgres", "env_only", "app", "app", database_url_env_var="APP_DATABASE_URL")[0]
    agent = AgentNode().build("Pi", "model", "manual", sql_database=sql)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]

    plan = Planner().build(deployment)

    assert [resource.role for resource in plan.resources] == ["agent"]
    assert plan.resources[0].pod_input["env"]["DATABASE_URL"] == "${APP_DATABASE_URL}"
