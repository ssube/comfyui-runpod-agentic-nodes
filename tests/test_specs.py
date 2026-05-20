import json
from pathlib import Path

from comfyui_runpod_agentic import NODE_DISPLAY_NAME_MAPPINGS
from comfyui_runpod_agentic.nodes import (
    AgentNode,
    BrowserNode,
    BuildContainerNode,
    DeployNode,
    DeployWithContainerdNode,
    LanguageRuntimeNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    MCPServerNode,
    NetworkStorageNode,
    PackageNode,
    RemoteSQLDatabaseNode,
    SkillFrameworkNode,
    SkillNode,
    SSHCommandNode,
)
from comfyui_runpod_agentic.setup_commands import harness_install_command
from comfyui_runpod_agentic.validation import ValidationError


def test_user_facing_core_node_names():
    assert NODE_DISPLAY_NAME_MAPPINGS["Deploy"] == "Deploy"
    assert NODE_DISPLAY_NAME_MAPPINGS["Package"] == "Package"
    assert NODE_DISPLAY_NAME_MAPPINGS["LanguageRuntime"] == "Language Runtime"
    assert NODE_DISPLAY_NAME_MAPPINGS["BuildContainer"] == "Build Container"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunOnRunpod"] == "Run on Runpod"
    assert NODE_DISPLAY_NAME_MAPPINGS["StartupScript"] == "Startup Script"
    assert NODE_DISPLAY_NAME_MAPPINGS["ComposeYAML"] == "Compose YAML"
    assert NODE_DISPLAY_NAME_MAPPINGS["DeployWithDocker"] == "Deploy with Docker"
    assert NODE_DISPLAY_NAME_MAPPINGS["DeployWithPodman"] == "Deploy with Podman"
    assert NODE_DISPLAY_NAME_MAPPINGS["DeployWithContainerd"] == "Deploy with Containerd"
    assert NODE_DISPLAY_NAME_MAPPINGS["RemoteSQLDatabase"] == "Remote SQL Database"
    assert NODE_DISPLAY_NAME_MAPPINGS["LocalSQLDatabase"] == "Local SQL Database"
    assert "RunpodPod" not in NODE_DISPLAY_NAME_MAPPINGS
    assert "RunpodSQLDatabase" not in NODE_DISPLAY_NAME_MAPPINGS


def test_llm_nodes_are_grouped_under_apis_category():
    assert LLMApiNode.CATEGORY == "Runpod/APIs"
    assert LLMServerNode.CATEGORY == "Runpod/APIs"


def test_local_runtime_nodes_expose_deployment_actions_only():
    action_choices = DeployWithContainerdNode.INPUT_TYPES()["required"]["action"][0]

    assert action_choices == ["save_only", "plan", "apply", "apply_and_wait", "stop", "terminate"]
    assert "config" not in action_choices
    assert "pull" not in action_choices


def test_ollama_deepseek_example_uses_setup_nodes_for_packages():
    workflow = json.loads(Path("examples/workflows/api_local_ollama_cloud_deepseek_agent_up.json").read_text())
    class_types = [node["class_type"] for node in workflow.values()]

    assert "LanguageRuntime" in class_types
    assert class_types.count("Package") == 2
    assert class_types.count("SSHCommand") == 1
    assert workflow["3"]["inputs"]["package_manager"] == "apt"
    assert workflow["4"]["inputs"]["package_manager"] == "npm"
    assert workflow["4"]["inputs"]["packages"] == "npm-check-updates"
    assert all("order" not in node["inputs"] for node in workflow.values())
    assert workflow["7"]["inputs"]["action"] == "apply_and_wait"


def test_container_snapshot_example_uses_build_container_plan():
    workflow = json.loads(Path("examples/workflows/api_container_snapshot_plan.json").read_text())
    class_types = [node["class_type"] for node in workflow.values()]

    assert "BuildContainer" in class_types
    assert workflow["3"]["inputs"]["previous"] == ["2", 0]
    assert workflow["3"]["inputs"]["push_to_docker_hub"] is False
    assert workflow["7"]["inputs"]["action"] == "plan"
    assert workflow["7"]["inputs"]["response_timeout_seconds"] == 0


def test_ollama_deepseek_ui_example_has_groups_and_positions():
    workflow = json.loads(Path("examples/workflows/ui_local_ollama_deepseek_setup.json").read_text())

    assert len(workflow["groups"]) == 4
    assert {group["title"] for group in workflow["groups"]} == {"Agent Inputs", "Container Setup", "Agent Deployment", "Local Runtime Execution"}
    assert all(isinstance(node.get("pos"), list) and len(node["pos"]) == 2 for node in workflow["nodes"])


def test_ui_examples_are_screenshot_ready_with_named_groups_and_positions():
    workflows = sorted(Path("examples/workflows").glob("ui_*.json"))

    assert workflows
    for path in workflows:
        workflow = json.loads(path.read_text())
        assert workflow.get("nodes"), path
        assert workflow.get("groups"), path
        assert all(group.get("title") for group in workflow["groups"]), path
        assert all(isinstance(node.get("pos"), list) and len(node["pos"]) == 2 for node in workflow["nodes"]), path
        assert all(isinstance(node.get("size"), list) and len(node["size"]) == 2 for node in workflow["nodes"]), path
        for index, first in enumerate(workflow["groups"]):
            for second in workflow["groups"][index + 1 :]:
                assert group_overlap_area(first["bounding"], second["bounding"]) == 0, path


def group_overlap_area(first: list[float], second: list[float]) -> float:
    first_x, first_y, first_width, first_height = first
    second_x, second_y, second_width, second_height = second
    overlap_width = max(0, min(first_x + first_width, second_x + second_width) - max(first_x, second_x))
    overlap_height = max(0, min(first_y + first_height, second_y + second_height) - max(first_y, second_y))
    return overlap_width * overlap_height


def test_agent_accepts_generic_llm_sources():
    llm_api = LLMApiNode().build("Claude", "claude-sonnet", "anthropic_key")[0]

    agent = AgentNode().build("OpenCode", "model", "manual", llm=llm_api)[0]

    assert agent.llm_api == llm_api
    assert agent.llm_server is None


def test_agent_installs_supported_harnesses_before_start():
    expected_packages = {
        "Codex": "@openai/codex",
        "Claude": "@anthropic-ai/claude-code",
        "OpenCode": "opencode-ai",
        "Hermes": "hermes-agent",
        "Pi": "@earendil-works/pi-coding-agent",
    }

    for harness, package in expected_packages.items():
        agent = AgentNode().build(harness, "model", "manual")[0]

        command = agent.runtime_contract.commands[0]
        assert command.phase == "before_start"
        assert command.source == f"harness:{harness.lower()}"
        assert package in command.command
        assert "--help >/dev/null" in command.command


def test_harness_install_commands_run_help_for_each_supported_cli():
    assert "codex --help >/dev/null" in harness_install_command("codex")
    assert "claude --help >/dev/null" in harness_install_command("claude")
    assert "opencode --help >/dev/null" in harness_install_command("opencode")
    assert "hermes --help >/dev/null" in harness_install_command("hermes")
    assert "pi --help >/dev/null" in harness_install_command("pi")


def test_sqlite_contract_is_file_only():
    spec = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]

    assert spec.materialization == "file_only"
    assert spec.runtime_contract.env.values["DATABASE_PATH"] == "/workspace/db/app.sqlite"
    assert spec.runtime_contract.env.values["DATABASE_URL"] == "sqlite:////workspace/db/app.sqlite"
    assert spec.runtime_contract.commands[0].source == "local_sql"
    assert "sqlite3" in spec.runtime_contract.commands[0].command


def test_package_node_chains_install_commands_and_apt_updates():
    apt = PackageNode().build("apt", "jq curl", "fail")[0]
    pip = PackageNode().build("pip", "pytest", "continue", previous=apt)[0]

    assert [command.order for command in pip.commands] == [0, 100]
    assert "apt-get update" in apt.commands[0].command
    assert "apt-get install" in apt.commands[0].command
    assert "python3 -m pip install pytest" in pip.commands[1].command


def test_command_node_inputs_do_not_expose_manual_order():
    assert "order" not in SSHCommandNode.INPUT_TYPES()["required"]
    assert "order" not in PackageNode.INPUT_TYPES()["required"]
    assert "order" not in LanguageRuntimeNode.INPUT_TYPES()["required"]
    assert "order" not in BuildContainerNode.INPUT_TYPES()["required"]


def test_language_runtime_node_installs_node_from_nodesource_and_python_from_apt():
    node = LanguageRuntimeNode().build("nodejs", 22)[0]
    python = LanguageRuntimeNode().build("python", 22, previous=node)[0]

    assert "https://deb.nodesource.com/node_22.x" in node.commands[0].command
    assert "python3-pip python3-venv pipx" in python.commands[1].command


def test_build_container_node_commits_and_pushes_with_dockerhub_env():
    snapshot = BuildContainerNode().build("docker.io/example/crag:latest", "nerdctl", True)[0]

    command = snapshot.commands[0]
    assert command.phase == "after_ready"
    assert "nerdctl" in command.command
    assert "commit \"$container_id\" \"$image_tag\"" in command.command
    assert "DOCKERHUB_USERNAME" in command.command
    assert "DOCKERHUB_TOKEN" in command.command
    assert "push \"$image_tag\"" in command.command


def test_command_nodes_ignore_legacy_order_argument_and_infer_from_chain():
    first = SSHCommandNode().build("echo first", "before_start", "fail", order=900)[0]
    second = SSHCommandNode().build("echo second", "before_start", "fail", previous=first, order=-900)[0]

    assert [command.order for command in second.commands] == [0, 100]


def test_browser_same_pod_adds_agent_capability():
    browser = BrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = AgentNode().build("OpenCode", "qwen", "manual", system_prompt="Use the browser only when needed.", browser=browser)[0]

    assert agent.required_image_capabilities == ["playwright"]
    assert agent.system_prompt == "Use the browser only when needed."


def test_service_nodes_accept_network_storage():
    storage = NetworkStorageNode().build("vol-123", "/data")[0]
    browser = BrowserNode().build("Neko", "own_pod", "chromium", network_storage=storage)[0]
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none", network_storage=storage)[0]
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app", network_storage=storage)[0]

    assert browser.network_storage == storage
    assert llm.network_storage == storage
    assert sql.network_storage == storage
    assert storage.retention_policy == "preserve"


def test_network_storage_retention_policy_warns_for_destructive_intent():
    storage = NetworkStorageNode().build("vol-123", "/workspace", "delete_with_deployment")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]

    deployment = DeployNode().build(agent, network_storage=storage)[0]

    assert deployment.network_storage.retention_policy == "delete_with_deployment"


def test_agent_accepts_mcp_servers():
    filesystem = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    github = MCPServerNode().build("github", "http", "", "", "https://mcp.example.test", '{"MODE":"read"}', "GITHUB_TOKEN", previous=filesystem)[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", mcp_servers=github)[0]

    assert len(agent.mcp_servers.servers) == 2
    assert "MCP_SERVERS_JSON" in agent.runtime_contract.env.values
    assert agent.runtime_contract.env.secrets[0].env_var == "GITHUB_TOKEN"


def test_agent_accepts_chainable_skills():
    skill = SkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    framework = SkillFrameworkNode().build("Superpowers", "", "", previous=skill)[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", skills=framework)[0]

    assert len(agent.skills.skills) == 2
    assert agent.skills.skills[1].kind == "framework"
    assert "RUNPOD_AGENT_SKILLS_JSON" in agent.runtime_contract.env.values
    assert [command.source for command in agent.runtime_contract.commands] == ["harness:pi", "skill:frontend-design", "skill:superpowers"]


def test_pod_validation_rejects_sqlite_outside_workspace():
    db = LocalSQLDatabaseNode().build("SQLite", "app", "/tmp/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", sql_database=db)[0]

    try:
        DeployNode().build(agent)
    except ValidationError as exc:
        assert "SQLite path" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_remote_sql_env_only_injects_database_url_from_server_env():
    spec = RemoteSQLDatabaseNode().build("Postgres", "env_only", "app", "app", database_url_env_var="APP_DATABASE_URL")[0]

    assert spec.materialization == "env_only"
    assert spec.template_key is None
    assert spec.runtime_contract.env.secrets[0].name == "APP_DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].env_var == "DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].provider == "server_env"
