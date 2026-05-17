from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodLLMApiNode,
    RunpodPodNode,
    RunpodSQLDatabaseNode,
)
from comfyui_runpod_agentic.validation import ValidationError


def test_agent_rejects_two_llm_sources():
    llm_api = RunpodLLMApiNode().build("Claude", "claude-sonnet", "anthropic_key")[0]
    llm_server = __import__("comfyui_runpod_agentic.nodes", fromlist=["RunpodLLMServerNode"]).RunpodLLMServerNode().build("vLLM", "Qwen/Qwen3-0.6B", "own_pod", "none")[0]

    try:
        RunpodAgentNode().build("OpenCode", "model", "manual", llm_api=llm_api, llm_server=llm_server)
    except ValidationError as exc:
        assert "either llm_api or llm_server" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_sqlite_contract_is_file_only():
    spec = RunpodSQLDatabaseNode().build("SQLite", "app", "app", sqlite_path="/workspace/db/app.sqlite")[0]

    assert spec.materialization == "file_only"
    assert spec.runtime_contract.env.values["DATABASE_URL"] == "sqlite:////workspace/db/app.sqlite"


def test_browser_same_pod_adds_agent_capability():
    browser = RunpodBrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = RunpodAgentNode().build("OpenCode", "qwen", "manual", browser=browser)[0]

    assert agent.required_image_capabilities == ["playwright"]


def test_pod_validation_rejects_sqlite_outside_workspace():
    db = RunpodSQLDatabaseNode().build("SQLite", "app", "app", sqlite_path="/tmp/app.sqlite")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", sql_database=db)[0]

    try:
        RunpodPodNode().build(agent)
    except ValidationError as exc:
        assert "SQLite path" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
