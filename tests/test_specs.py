from comfyui_runpod_agentic import NODE_DISPLAY_NAME_MAPPINGS
from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodLLMApiNode,
    RunpodLLMServerNode,
    RunpodLocalSQLDatabaseNode,
    RunpodMCPServerNode,
    RunpodNetworkStorageNode,
    RunpodPodNode,
    RunpodRemoteSQLDatabaseNode,
    RunpodSkillFrameworkNode,
    RunpodSkillNode,
)
from comfyui_runpod_agentic.validation import ValidationError


def test_user_facing_core_node_names():
    assert NODE_DISPLAY_NAME_MAPPINGS["RunpodPod"] == "Runpod Pod"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunpodRun"] == "Run on Runpod"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunpodRemoteSQLDatabase"] == "Remote SQL Database"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunpodLocalSQLDatabase"] == "Local SQL Database"
    assert "RunpodSQLDatabase" not in NODE_DISPLAY_NAME_MAPPINGS


def test_agent_accepts_generic_llm_sources():
    llm_api = RunpodLLMApiNode().build("Claude", "claude-sonnet", "anthropic_key")[0]

    agent = RunpodAgentNode().build("OpenCode", "model", "manual", llm=llm_api)[0]

    assert agent.llm_api == llm_api
    assert agent.llm_server is None


def test_sqlite_contract_is_file_only():
    spec = RunpodLocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]

    assert spec.materialization == "file_only"
    assert spec.runtime_contract.env.values["DATABASE_PATH"] == "/workspace/db/app.sqlite"
    assert spec.runtime_contract.env.values["DATABASE_URL"] == "sqlite:////workspace/db/app.sqlite"
    assert spec.runtime_contract.commands[0].source == "local_sql"
    assert "sqlite3" in spec.runtime_contract.commands[0].command


def test_browser_same_pod_adds_agent_capability():
    browser = RunpodBrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = RunpodAgentNode().build("OpenCode", "qwen", "manual", system_prompt="Use the browser only when needed.", browser=browser)[0]

    assert agent.required_image_capabilities == ["playwright"]
    assert agent.system_prompt == "Use the browser only when needed."


def test_service_nodes_accept_network_storage():
    storage = RunpodNetworkStorageNode().build("vol-123", "/data")[0]
    browser = RunpodBrowserNode().build("Neko", "own_pod", "chromium", network_storage=storage)[0]
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none", network_storage=storage)[0]
    sql = RunpodRemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app", network_storage=storage)[0]

    assert browser.network_storage == storage
    assert llm.network_storage == storage
    assert sql.network_storage == storage


def test_agent_accepts_mcp_servers():
    filesystem = RunpodMCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    github = RunpodMCPServerNode().build("github", "http", "", "", "https://mcp.example.test", '{"MODE":"read"}', "GITHUB_TOKEN", previous=filesystem)[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", mcp_servers=github)[0]

    assert len(agent.mcp_servers.servers) == 2
    assert "MCP_SERVERS_JSON" in agent.runtime_contract.env.values
    assert agent.runtime_contract.env.secrets[0].env_var == "GITHUB_TOKEN"


def test_agent_accepts_chainable_skills():
    skill = RunpodSkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    framework = RunpodSkillFrameworkNode().build("Superpowers", "", "", previous=skill)[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", skills=framework)[0]

    assert len(agent.skills.skills) == 2
    assert agent.skills.skills[1].kind == "framework"
    assert "RUNPOD_AGENT_SKILLS_JSON" in agent.runtime_contract.env.values
    assert [command.source for command in agent.runtime_contract.commands] == ["skill:frontend-design", "skill:superpowers"]


def test_pod_validation_rejects_sqlite_outside_workspace():
    db = RunpodLocalSQLDatabaseNode().build("SQLite", "app", "/tmp/app.sqlite")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", sql_database=db)[0]

    try:
        RunpodPodNode().build(agent)
    except ValidationError as exc:
        assert "SQLite path" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_remote_sql_env_only_injects_database_url_from_server_env():
    spec = RunpodRemoteSQLDatabaseNode().build("Postgres", "env_only", "app", "app", database_url_env_var="APP_DATABASE_URL")[0]

    assert spec.materialization == "env_only"
    assert spec.template_key is None
    assert spec.runtime_contract.env.secrets[0].name == "APP_DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].env_var == "DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].provider == "server_env"
