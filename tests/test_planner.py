import json

from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodKeepAliveNode,
    RunpodLLMApiNode,
    RunpodPodNode,
    RunpodSQLDatabaseNode,
    RunpodSSHCommandNode,
    RunpodVectorDatabaseNode,
)
from comfyui_runpod_agentic.planner import Planner


def build_deployment():
    llm = RunpodLLMApiNode().build("Claude", "claude-sonnet", "anthropic_key")[0]
    sql = RunpodSQLDatabaseNode().build("Postgres", "app", "app", "pg_password")[0]
    vector = RunpodVectorDatabaseNode().build("Qdrant", "docs")[0]
    browser = RunpodBrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = RunpodAgentNode().build("OpenCode", "claude-sonnet", "wait_for_commands", browser=browser, llm_api=llm, sql_database=sql, vector_database=vector, node_id="agent1")[0]
    commands = RunpodSSHCommandNode().build("echo setup", "before_start", 10, "fail")[0]
    keep_alive = RunpodKeepAliveNode().build("time", "stop", 30, "minutes", 0, 0.0, 0)[0]
    return RunpodPodNode().build(agent, gpu_type_id="NVIDIA A40", commands=commands, keep_alive=keep_alive)[0]


def test_planner_orders_dependencies_before_agent():
    plan = Planner().build(build_deployment(), prompt={"id": "prompt"})
    actions = [action.action for action in plan.actions]

    assert actions.index("RESOLVE_DEPENDENCY_CONTRACTS") < actions.index("CREATE_OR_RESUME", 2)
    assert [resource.role for resource in plan.resources] == ["sql", "vector", "agent"]
    assert plan.resources[-1].template_id == "rp-agent-opencode-playwright"


def test_plan_is_json_serializable():
    plan = Planner().build(build_deployment())

    encoded = json.dumps(plan.to_dict(), sort_keys=True)

    assert "WRITE_RUNTIME_CONFIG" in encoded
    assert "ANTHROPIC_API_KEY" in encoded
