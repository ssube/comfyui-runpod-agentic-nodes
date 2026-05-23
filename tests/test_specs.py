import json
from pathlib import Path

import pytest

from comfyui_runpod_agentic import NODE_DISPLAY_NAME_MAPPINGS
from comfyui_runpod_agentic.harnesses import CENTRAL_SKILLS_PATH, harness_matrix_rows
from comfyui_runpod_agentic.local_runtime import LocalApplyResult
from comfyui_runpod_agentic.nodes import (
    AgentNode,
    BrowserNode,
    BuildContainerNode,
    DeployNode,
    KeepAliveNode,
    LanguageRuntimeNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    MCPServerNode,
    NetworkStorageNode,
    PackageNode,
    RemoteSQLDatabaseNode,
    RunLocalContainersNode,
    RunOnRunpodNode,
    S3StorageNode,
    SkillFrameworkNode,
    SkillNode,
    SSHCommandNode,
    StartupScriptNode,
    VectorDatabaseNode,
    WebTerminalNode,
    local_terminal_auth,
    local_terminal_urls,
    with_terminal_options,
)
from comfyui_runpod_agentic.planner import Planner
from comfyui_runpod_agentic.setup_commands import builtin_database_skill_files, container_snapshot_command, harness_install_command
from comfyui_runpod_agentic.validation import ValidationError, validate_keep_alive


def test_user_facing_core_node_names():
    assert NODE_DISPLAY_NAME_MAPPINGS["Deploy"] == "Deploy"
    assert NODE_DISPLAY_NAME_MAPPINGS["Package"] == "Package"
    assert NODE_DISPLAY_NAME_MAPPINGS["LanguageRuntime"] == "Language Runtime"
    assert NODE_DISPLAY_NAME_MAPPINGS["BuildContainer"] == "Build Container"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunOnRunpod"] == "Run on Runpod"
    assert NODE_DISPLAY_NAME_MAPPINGS["StartupScript"] == "Startup Script"
    assert NODE_DISPLAY_NAME_MAPPINGS["ComposeYAML"] == "Compose YAML"
    assert NODE_DISPLAY_NAME_MAPPINGS["RunLocalContainers"] == "Run Local Containers"
    assert NODE_DISPLAY_NAME_MAPPINGS["WebTerminal"] == "Web Terminal"
    assert NODE_DISPLAY_NAME_MAPPINGS["RemoteSQLDatabase"] == "Remote SQL Database"
    assert NODE_DISPLAY_NAME_MAPPINGS["LocalSQLDatabase"] == "Local SQL Database"
    assert "RunpodPod" not in NODE_DISPLAY_NAME_MAPPINGS
    assert "RunpodSQLDatabase" not in NODE_DISPLAY_NAME_MAPPINGS


def test_llm_nodes_are_grouped_under_apis_category():
    assert LLMApiNode.CATEGORY == "Runpod/APIs"
    assert LLMServerNode.CATEGORY == "Runpod/APIs"


def test_local_runtime_nodes_expose_deployment_actions_only():
    action_choices = RunLocalContainersNode.INPUT_TYPES()["required"]["action"][0]

    assert action_choices == ["save_only", "plan", "apply", "apply_and_wait", "stop", "terminate"]
    assert "config" not in action_choices
    assert "pull" not in action_choices


def test_terminal_run_nodes_put_prompt_first():
    assert next(iter(RunOnRunpodNode.INPUT_TYPES()["required"])) == "prompt"
    assert next(iter(RunLocalContainersNode.INPUT_TYPES()["required"])) == "prompt"


def test_deploy_is_graph_only_and_runpod_terminal_owns_placement_options():
    deploy_required = DeployNode.INPUT_TYPES()["required"]
    deploy_optional = DeployNode.INPUT_TYPES()["optional"]
    runpod_required = RunOnRunpodNode.INPUT_TYPES()["required"]
    local_required = RunLocalContainersNode.INPUT_TYPES()["required"]

    assert list(deploy_required) == ["app"]
    assert list(deploy_optional) == ["network_storage", "s3_storage", "commands", "keep_alive"]
    assert "ssh_access" not in deploy_optional
    assert {"gpu_type_id", "gpu_count", "vcpu_count", "cloud_type", "container_disk_gb", "volume_gb", "expose_public_ip", "reuse_policy"} <= set(runpod_required)
    assert "ssh_access" in RunOnRunpodNode.INPUT_TYPES()["optional"]
    assert "engine" in local_required
    assert "reuse_policy" in local_required
    assert not {"gpu_type_id", "gpu_count", "cloud_type", "container_disk_gb", "volume_gb", "expose_public_ip"} & set(local_required)


def test_cpu_runpod_placement_omits_gpu_type_id():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = with_terminal_options(DeployNode().build(agent)[0], gpu_type_id="CPU", gpu_count=0, vcpu_count=4, cloud_type="SECURE")

    plan = Planner().build(deployment, mode="plan")

    assert deployment.resource_hints.cpu_only is True
    assert deployment.resource_hints.gpu_type_id is None
    assert deployment.resource_hints.vcpu_count == 4
    assert plan.resources[0].pod_input["computeType"] == "CPU"
    assert plan.resources[0].pod_input["minVcpuCount"] == 4
    assert "gpuCount" not in plan.resources[0].pod_input
    assert "gpuTypeId" not in plan.resources[0].pod_input


def test_cpu_runpod_placement_uses_default_vcpu_count():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = with_terminal_options(DeployNode().build(agent)[0], gpu_count=0)

    plan = Planner().build(deployment, mode="plan")

    assert plan.resources[0].pod_input["minVcpuCount"] == 2


def test_web_terminal_adds_ttyd_contract_to_agent():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]

    assert agent.terminal == terminal
    assert agent.runtime_contract.ports[0].name == "terminal"
    assert agent.runtime_contract.ports[0].container_port == 7681
    assert "ttyd" in agent.runtime_contract.commands[-1].command
    assert agent.runtime_contract.commands[-1].failure_policy == "continue"
    assert agent.runtime_contract.env.values["CRAG_WEB_TERMINAL_HOST_PORT"] == "8765"


def test_web_terminal_shell_supports_commands_with_arguments():
    terminal = WebTerminalNode().build("tmux attach -t crag-pi", 7681, 8765, "none", "crag", "secret")[0]

    assert "/bin/bash -lc 'tmux attach -t crag-pi'" in terminal.runtime_contract.commands[0].command


def test_web_terminal_password_mode_requires_password():
    try:
        WebTerminalNode().build("/bin/bash", 7681, 7681, "password", "crag", "")
    except ValidationError as exc:
        assert "password is required" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_app_nodes_reject_unsupported_modes():
    with pytest.raises(ValidationError, match="Neko browser"):
        BrowserNode().build("Neko", "same_pod", "chromium")
    with pytest.raises(ValidationError, match="Local SQL Database only supports SQLite"):
        LocalSQLDatabaseNode().build("Postgres", "app", "/workspace/db/app.sqlite")


def test_run_nodes_emit_comfy_ui_text_when_called_by_graph(tmp_path):
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    deployment = DeployNode().build(agent)[0]

    result = RunLocalContainersNode().apply(deployment, action="plan", output_path=str(tmp_path / "compose.yaml"), workflow_graph={})

    assert result["ui"]["text"]
    assert result["result"][0] == result["ui"]["text"][0]
    payload = json.loads(result["result"][0])
    assert "terminal_auth" not in payload


def test_runpod_node_plan_and_apply_outputs(monkeypatch):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]

    plan_output = RunOnRunpodNode().run(deployment, mode="plan", prompt="hello", workflow_graph={})
    plan_payload = json.loads(plan_output["result"][0])

    assert plan_output["ui"]["response"] == [""]
    assert plan_payload["mode"] == "plan"

    class FakeRunpodRunner:
        def __init__(self, progress=None):
            self.progress = progress

        def run(self, deployment, *, mode, prompt, workflow_graph, on_error):
            assert mode == "apply"
            assert prompt == "hello"
            assert workflow_graph == {}
            assert on_error == "terminate_created"
            return {"status": "completed", "response": "agent reply", "errors": ""}

    monkeypatch.setattr("comfyui_runpod_agentic.runner.RunpodRunner", FakeRunpodRunner)

    apply_output = RunOnRunpodNode().run(deployment, mode="apply", prompt="hello", on_error="terminate_created", workflow_graph={})

    assert apply_output["ui"]["response"] == ["agent reply"]
    assert json.loads(apply_output["result"][0])["status"] == "completed"


def test_runpod_node_reports_runner_errors(monkeypatch):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]

    class FailingRunpodRunner:
        def __init__(self, progress=None):
            pass

        def run(self, deployment, *, mode, prompt, workflow_graph, on_error):
            raise RuntimeError("boom")

    monkeypatch.setattr("comfyui_runpod_agentic.runner.RunpodRunner", FailingRunpodRunner)

    output = RunOnRunpodNode().run(deployment, mode="apply", workflow_graph={})

    assert output["ui"]["errors"] == ["boom"]
    assert json.loads(output["result"][0])["status"] == "failed"


def test_stop_and_terminate_do_not_emit_terminal_urls(tmp_path):
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    deployment = DeployNode().build(agent)[0]

    result = RunLocalContainersNode().apply(deployment, action="terminate", output_path=str(tmp_path / "compose.yaml"), workflow_graph={})

    payload = json.loads(result["result"][0])
    assert "terminal_urls" not in payload
    assert "terminal_auth" not in payload


def test_local_terminal_helpers_return_proxy_ready_urls_and_auth():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    plan = Planner().build(DeployNode().build(agent)[0])

    assert local_terminal_urls(plan) == {"agent": "http://127.0.0.1:8765"}
    assert local_terminal_auth(plan) == {"agent": {"username": "crag", "password": "secret"}}


def test_run_nodes_are_not_cached_by_comfy():
    assert RunLocalContainersNode.IS_CHANGED() != RunLocalContainersNode.IS_CHANGED()
    assert RunOnRunpodNode.IS_CHANGED() != RunOnRunpodNode.IS_CHANGED()


def test_frontend_terminal_uses_overlay_without_embedded_widget():
    script = Path("web/optional_frontend_extensions.js").read_text()

    assert "showFloatingTerminal(terminal)" in script
    assert "Open Web Terminal" in script
    assert 'api.addEventListener("executed"' in script
    assert "graphNodeById" in script
    assert "addDOMWidget" not in script


def test_runpod_catalog_options_become_dropdowns(monkeypatch):
    monkeypatch.setattr(
        "comfyui_runpod_agentic.nodes.runpod_dropdown_options",
        lambda: type("Options", (), {"gpu_type_ids": ["NVIDIA RTX A4000"], "data_center_ids": ["US-KS-2"]})(),
    )

    runpod_required = RunOnRunpodNode.INPUT_TYPES()["required"]
    storage_required = NetworkStorageNode.INPUT_TYPES()["required"]

    assert runpod_required["gpu_type_id"] == (["", "NVIDIA RTX A4000"],)
    assert storage_required["data_center_id"] == (["", "US-KS-2"],)


def test_ollama_deepseek_example_uses_setup_nodes_for_packages():
    workflow = json.loads(Path("examples/workflows/api_local_ollama_cloud_deepseek_agent.json").read_text())
    class_types = [node["class_type"] for node in workflow.values()]

    assert "LanguageRuntime" in class_types
    assert class_types.count("Package") == 2
    assert class_types.count("SSHCommand") == 1
    assert workflow["3"]["inputs"]["package_manager"] == "apt"
    assert workflow["4"]["inputs"]["package_manager"] == "npm"
    assert workflow["4"]["inputs"]["packages"] == "npm-check-updates"
    assert all("order" not in node["inputs"] for node in workflow.values())
    assert workflow["7"]["inputs"]["action"] == "apply_and_wait"


def test_pi_ollama_terminal_example_attaches_ttyd_to_tmux_session():
    workflow = json.loads(Path("examples/workflows/api_local_pi_ollama_terminal.json").read_text())
    class_types = [node["class_type"] for node in workflow.values()]

    assert "WebTerminal" in class_types
    assert "LLMApi" in class_types
    assert workflow["1"]["inputs"]["shell"] == "while [ ! -f /workspace/.runpod_agentic/startup.ready ]; do sleep 1; done; exec tmux attach -t crag-pi"
    assert workflow["4"]["inputs"]["phase"] == "before_start"
    assert "tmux new-session -d -s crag-pi" in workflow["4"]["inputs"]["command"]
    assert "pi --provider ollama-cloud" in workflow["4"]["inputs"]["command"]
    assert workflow["5"]["inputs"]["provider"] == "Ollama Cloud"
    assert workflow["6"]["inputs"]["startup_mode"] == "manual"
    assert workflow["8"]["inputs"]["action"] == "apply"


def test_container_snapshot_example_uses_build_container_plan():
    workflow = json.loads(Path("examples/workflows/api_container_snapshot_plan.json").read_text())
    class_types = [node["class_type"] for node in workflow.values()]

    assert "BuildContainer" in class_types
    assert "RunLocalContainers" not in class_types
    assert workflow["3"]["inputs"]["deployment"] == ["6", 0]
    assert "previous" not in workflow["3"]["inputs"]
    assert workflow["3"]["inputs"]["container_runtime"] == "nerdctl"
    assert workflow["3"]["inputs"]["push_to_docker_hub"] is False
    assert workflow["6"]["inputs"]["commands"] == ["2", 0]


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
        assert all(any(group_contains_node(group["bounding"], node["pos"], node["size"]) for group in workflow["groups"]) for node in workflow["nodes"]), path
        for index, first in enumerate(workflow["groups"]):
            for second in workflow["groups"][index + 1 :]:
                assert group_overlap_area(first["bounding"], second["bounding"]) == 0, path


def test_ui_examples_match_terminal_widget_order():
    for path in sorted(Path("examples/workflows").glob("ui_*.json")):
        workflow = json.loads(path.read_text())
        for node in workflow["nodes"]:
            values = node.get("widgets_values") or []
            if node["type"] == "RunOnRunpod":
                assert len(values) == 11, path
                assert isinstance(values[0], str) and values[1] in {"plan", "apply", "apply_and_wait", "stop", "terminate"}, path
                assert values[4] in {"auto", "SECURE", "COMMUNITY"}, path
                assert values[9] in {"stop_created", "terminate_created", "leave_running"}, path
                assert values[10] in {"info", "debug"}, path
            if node["type"] == "RunLocalContainers":
                assert len(values) == 11, path
                assert isinstance(values[0], str) and values[1] in {"containerd", "docker", "podman"}, path
                assert values[4] in {"save_only", "plan", "apply", "apply_and_wait", "stop", "terminate"}, path
                assert values[10] in {"reuse_matching", "always_create", "resume_stopped"}, path


def group_contains_node(group: list[float], position: list[float], size: list[float]) -> bool:
    group_x, group_y, group_width, group_height = group
    node_x, node_y = position
    node_width, node_height = size
    return group_x <= node_x and group_y <= node_y and node_x + node_width <= group_x + group_width and node_y + node_height <= group_y + group_height


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


def test_llm_api_base_url_overrides_provider_defaults():
    codex = LLMApiNode().build("Codex", "gpt-test", "OPENAI_KEY", "https://openai-proxy.example/v1")[0]
    claude = LLMApiNode().build("Claude", "claude-test", "ANTHROPIC_KEY", "https://anthropic-proxy.example")[0]
    ollama = LLMApiNode().build("Ollama Cloud", "deepseek-test", "OLLAMA_KEY", "https://ollama-proxy.example")[0]

    assert codex.runtime_contract.env.values["OPENAI_BASE_URL"] == "https://openai-proxy.example/v1"
    assert claude.runtime_contract.env.values["LLM_API_BASE_URL"] == "https://anthropic-proxy.example"
    assert "OPENAI_BASE_URL" not in claude.runtime_contract.env.values
    assert ollama.runtime_contract.env.values["OLLAMA_HOST"] == "https://ollama-proxy.example"


def test_agent_installs_supported_harnesses_before_start():
    expected_packages = {
        "Codex": "@openai/codex",
        "Claude": "@anthropic-ai/claude-code",
        "OpenCode": "opencode-ai",
        "Hermes": "hermes-agent",
        "Pi": "@earendil-works/pi-coding-agent",
    }

    for harness, package in expected_packages.items():
        agent = AgentNode().build(harness, "model", "auto_start")[0]

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


def test_agent_can_skip_harness_install_for_local_e2e(monkeypatch):
    monkeypatch.setenv("CRAG_SKIP_HARNESS_INSTALL", "1")

    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]

    assert agent.runtime_contract.commands == []


def test_agent_warns_when_system_prompt_is_not_supported_by_harness():
    agent = AgentNode().build("Codex", "model", "manual", system_prompt="Be brief.")[0]
    supported = AgentNode().build("Pi", "model", "manual", system_prompt="Be brief.")[0]

    assert "does not advertise system prompt support" in agent.runtime_contract.env.values["CRAG_AGENT_WARNINGS"]
    assert "CRAG_AGENT_WARNINGS" not in supported.runtime_contract.env.values


def test_manual_agent_skips_harness_install_and_terminal_runs_first():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    manual_agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    auto_agent = AgentNode().build("Pi", "model", "auto_start", terminal=terminal)[0]
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY")[0]
    configured_manual_agent = AgentNode().build("Pi", "model", "manual", terminal=terminal, llm=llm)[0]

    assert [command.source for command in manual_agent.runtime_contract.commands] == ["web_terminal"]
    ordered = sorted(auto_agent.runtime_contract.commands, key=lambda command: command.order)
    assert [command.source for command in ordered] == ["web_terminal", "harness:pi"]
    assert next(command for command in auto_agent.runtime_contract.commands if command.source == "harness:pi").failure_policy == "continue"
    configured_ordered = sorted(configured_manual_agent.runtime_contract.commands, key=lambda command: command.order)
    assert [command.source for command in configured_ordered] == ["web_terminal", "harness:pi"]


def test_terminal_active_agent_keeps_skill_startup_commands():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    skill = SkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    agent = AgentNode().build("Pi", "model", "auto_start", terminal=terminal, skills=skill)[0]
    ordered = sorted(agent.runtime_contract.commands, key=lambda command: command.order)

    assert [command.source for command in ordered] == ["web_terminal", "harness:pi", "skill:frontend-design"]
    assert ordered[0].failure_policy == "continue"


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
    inputs = BuildContainerNode.INPUT_TYPES()["required"]
    command = container_snapshot_command("docker.io/example/crag:latest", "nerdctl", True, "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN")

    assert "deployment" in inputs
    assert "container_runtime" in inputs
    assert BuildContainerNode.OUTPUT_NODE is True
    assert "nerdctl" in command
    assert "commit \"$container_id\" \"$image_tag\"" in command
    assert "DOCKERHUB_USERNAME" in command
    assert "DOCKERHUB_TOKEN" in command
    assert "push \"$image_tag\"" in command


def test_build_container_node_returns_named_comfy_outputs(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    output_path = tmp_path / "build.yaml"

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.compose_yaml_for_plan", lambda *_args, **_kwargs: "services: {}\n")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.write_compose_file", lambda *_args, **_kwargs: str(output_path))
    monkeypatch.setattr(
        "comfyui_runpod_agentic.local_runtime.apply_local_runtime_plan",
        lambda *_args, **_kwargs: (LocalApplyResult("containerd", "apply_and_wait", str(output_path), ["nerdctl"], 0, "built\n", ""), False),
    )

    output = BuildContainerNode().apply(deployment, "example/crag:latest", output_path=str(output_path), workflow_graph={"nodes": []})

    assert output["ui"]["response"] == ["built\n"]
    assert output["ui"]["saved_path"] == [str(output_path)]


def test_command_nodes_ignore_legacy_order_argument_and_infer_from_chain():
    first = SSHCommandNode().build("echo first", "before_start", "fail", order=900)[0]
    second = SSHCommandNode().build("echo second", "before_start", "fail", previous=first, order=-900)[0]

    assert [command.order for command in second.commands] == [0, 100]


def test_browser_same_pod_adds_agent_capability():
    browser = BrowserNode().build("Playwright", "same_pod", "chromium")[0]
    agent = AgentNode().build("OpenCode", "qwen", "manual", system_prompt="Use the browser only when needed.", browser=browser)[0]

    assert agent.required_image_capabilities == ["playwright"]
    assert agent.system_prompt == "Use the browser only when needed."


def test_llm_server_same_pod_adds_agent_capability_and_runtime_contract():
    llm = LLMServerNode().build("Ollama", "llama3.2", "same_pod", "none")[0]
    agent = AgentNode().build("Pi", "llama3.2", "manual", llm=llm)[0]
    deployment = DeployNode().build(agent)[0]

    plan = Planner().build(deployment)

    assert llm.materialization == "same_pod"
    assert llm.template_key is None
    assert agent.required_image_capabilities == ["ollama"]
    assert [resource.role for resource in plan.resources] == ["agent"]
    assert plan.resources[0].pod_input["env"]["OLLAMA_HOST"] == "http://127.0.0.1:11434"
    assert any(action.detail.get("source") == "llm:ollama:same_pod" for action in plan.actions if action.action == "RUN_SSH_COMMAND")


def test_browser_own_pod_playwright_exposes_remote_endpoint():
    browser = BrowserNode().build("Playwright", "own_pod", "firefox", node_id="browser-1")[0]

    assert browser.materialization == "own_pod"
    assert browser.template_key == "rp-browser-playwright"
    assert browser.runtime_contract.env.values["PLAYWRIGHT_MODE"] == "remote"
    assert browser.runtime_contract.env.values["PLAYWRIGHT_WS_ENDPOINT"] == "crag://browser/playwright"
    assert browser.runtime_contract.ports[0].container_port == 3000
    assert browser.meta.node_id == "browser-1"


def test_service_nodes_accept_network_storage():
    storage = NetworkStorageNode().build("vol-123", "/data")[0]
    browser = BrowserNode().build("Neko", "own_pod", "chromium", network_storage=storage)[0]
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none", network_storage=storage)[0]
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app", network_storage=storage)[0]

    assert browser.network_storage == storage
    assert llm.network_storage == storage
    assert sql.network_storage == storage
    assert storage.retention_policy == "preserve"


def test_vllm_server_uses_openai_contract_and_optional_secrets():
    llm = LLMServerNode().build("vLLM", "Qwen/Qwen3", "own_pod", "secret", "OPENAI_KEY", "HF_TOKEN", node_id="llm-1")[0]

    assert llm.engine == "vllm"
    assert llm.api_format == "openai"
    assert llm.runtime_contract.env.values["OPENAI_BASE_URL"] == "crag://llm/vllm/v1"
    assert llm.runtime_contract.env.values["OPENAI_MODEL"] == "Qwen/Qwen3"
    assert [secret.env_var for secret in llm.runtime_contract.env.secrets] == ["OPENAI_API_KEY", "HF_TOKEN"]
    assert [secret.name for secret in llm.runtime_contract.env.secrets] == ["OPENAI_KEY", "HF_TOKEN"]
    assert llm.runtime_contract.ports[0].container_port == 8000
    assert llm.meta.node_id == "llm-1"


def test_ollama_server_generated_token_sets_openai_key_placeholder():
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "generated_token")[0]

    assert llm.runtime_contract.env.values["OPENAI_API_KEY"] == "crag-generated-at-apply"
    assert llm.runtime_contract.env.secrets == []


def test_vector_database_contract_includes_persistence_path():
    vector = VectorDatabaseNode().build("Chroma", "docs", "/data/chroma")[0]

    assert vector.engine == "chroma"
    assert vector.runtime_contract.env.values["VECTOR_URL"] == "crag://vector/chroma"
    assert vector.runtime_contract.env.values["VECTOR_MODE"] == "remote"
    assert vector.runtime_contract.env.values["VECTOR_PERSISTENCE_PATH"] == "/data/chroma"
    assert vector.runtime_contract.ports[0].container_port == 8000


def test_embedded_chroma_is_file_only_and_installs_skill():
    vector = VectorDatabaseNode().build("Chroma", "embedded", "docs", "/workspace/vector")[0]
    agent = AgentNode().build("Pi", "model", "manual", vector_database=vector)[0]
    plan = Planner().build(DeployNode().build(agent)[0])

    assert vector.materialization == "file_only"
    assert vector.template_key is None
    assert vector.runtime_contract.env.values["VECTOR_MODE"] == "embedded"
    assert "chromadb" in vector.runtime_contract.commands[0].command
    assert [resource.role for resource in plan.resources] == ["agent"]
    assert plan.resources[0].pod_input["env"]["VECTOR_URL"] == "local://chroma"


def test_builtin_database_skill_files_use_agent_skills_frontmatter():
    files = builtin_database_skill_files()
    skill = files[f"{CENTRAL_SKILLS_PATH}/crag-database/SKILL.md"]

    assert skill.startswith("---\nname: crag-database\n")
    assert "description:" in skill
    assert "python3 $CRAG_RUNTIME_DIR/skills/crag-database/list_resources.py" in skill
    assert f"{CENTRAL_SKILLS_PATH}/crag-database/list_resources.py" in files


def test_network_storage_retention_policy_warns_for_destructive_intent():
    storage = NetworkStorageNode().build("vol-123", "/workspace", "delete_with_deployment")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]

    deployment = DeployNode().build(agent, network_storage=storage)[0]

    assert deployment.network_storage.retention_policy == "delete_with_deployment"


def test_s3_storage_merges_server_env_secret_contract():
    storage = S3StorageNode().build("https://s3.example.test", "bucket", "us-east-1", "ACCESS_ENV", "SECRET_ENV")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]

    deployment = DeployNode().build(agent, s3_storage=storage)[0]
    plan = Planner().build(deployment, mode="plan")

    assert plan.runtime_contract.env.values["S3_BUCKET"] == "bucket"
    assert [secret.env_var for secret in plan.runtime_contract.env.secrets] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]


def test_startup_script_node_exports_prompted_launcher_sequence():
    agent = AgentNode().build("Pi", "model", "wait_for_commands", system_prompt="Stay concise.")[0]
    deployment = DeployNode().build(agent)[0]

    script = StartupScriptNode().export(deployment, prompt="Say hello.")[0]

    assert script.startswith("bash <<'CRAG_STARTUP'")
    assert ".runpod_agentic/launcher.sh" in script
    assert "Say hello." in script
    assert "Stay concise." in script


def test_agent_accepts_mcp_servers():
    filesystem = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    github = MCPServerNode().build("github", "http", "", "", "https://mcp.example.test", '{"MODE":"read"}', "GITHUB_TOKEN", previous=filesystem)[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", mcp_servers=github)[0]

    assert len(agent.mcp_servers.servers) == 2
    assert "MCP_SERVERS_JSON" in agent.runtime_contract.env.values
    assert agent.runtime_contract.env.secrets[0].env_var == "GITHUB_TOKEN"


def test_mcp_node_rejects_invalid_transport_inputs():
    with pytest.raises(ValidationError, match="name is required"):
        MCPServerNode().build("", "stdio", "npx", "", "", "{}")
    with pytest.raises(ValidationError, match="JSON object"):
        MCPServerNode().build("bad", "stdio", "npx", "", "", "[]")
    with pytest.raises(ValidationError, match="stdio transport requires"):
        MCPServerNode().build("bad", "stdio", "", "", "", "{}")
    with pytest.raises(ValidationError, match="http/sse transport requires"):
        MCPServerNode().build("bad", "http", "", "", "", "{}")


def test_agent_accepts_chainable_skills():
    skill = SkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    framework = SkillFrameworkNode().build("Superpowers", "", "", previous=skill)[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", skills=framework)[0]

    assert len(agent.skills.skills) == 2
    assert agent.skills.skills[0].target_path == f"{CENTRAL_SKILLS_PATH}/frontend-design"
    assert agent.skills.skills[1].target_path == CENTRAL_SKILLS_PATH
    assert agent.skills.skills[1].kind == "framework"
    assert "RUNPOD_AGENT_SKILLS_JSON" in agent.runtime_contract.env.values
    assert [command.source for command in agent.runtime_contract.commands] == ["harness:pi", "skill:frontend-design", "skill:superpowers"]


def test_skill_nodes_reject_invalid_repositories():
    with pytest.raises(ValidationError, match="Skill name is required"):
        SkillNode().build("", "https://github.com/example/skills.git", ".")
    with pytest.raises(ValidationError, match="Skill GitHub repo URL"):
        SkillNode().build("skill", "https://example.com/skills.git", ".")
    with pytest.raises(ValidationError, match="Skill framework GitHub repo URL"):
        SkillFrameworkNode().build("Custom GitHub Repo", "https://example.com/skills.git", ".")


def test_harness_compatibility_matrix_covers_agent_choices():
    rows = harness_matrix_rows()

    assert [row["harness"] for row in rows] == ["Codex", "Claude", "OpenCode", "Hermes", "Pi"]
    assert all(row["prompt"] for row in rows)
    assert all(row["skills_symlink"] for row in rows)
    assert all(row["response_capture"] for row in rows)


def test_pod_validation_rejects_sqlite_outside_workspace():
    db = LocalSQLDatabaseNode().build("SQLite", "app", "/tmp/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", sql_database=db)[0]

    try:
        DeployNode().build(agent)
    except ValidationError as exc:
        assert "SQLite path" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_validation_rejects_invalid_graph_combinations():
    llm_api = LLMApiNode().build("Codex", "gpt-test", "OPENAI_KEY")[0]
    llm_server = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = AgentNode().build("Pi", "model", "manual", llm=llm_api)[0]
    bad_agent = agent.__class__(**{**agent.__dict__, "llm_server": llm_server})

    try:
        DeployNode().build(bad_agent)
    except ValidationError as exc:
        assert "either llm_api or llm_server" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_validation_warns_for_ephemeral_sqlite_and_dependency_installs():
    sqlite = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", sql_database=sqlite)[0]
    command = SSHCommandNode().build("apt-get install -y jq", "before_start", "fail")[0]
    deployment = DeployNode().build(agent, commands=command)[0]

    plan = Planner().build(deployment)

    assert "SQLite without network storage may be ephemeral." in plan.warnings
    assert "Startup command appears to install dependencies" in "\n".join(plan.warnings)


def test_validation_rejects_bad_storage_and_s3_secret_contracts():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    bad_storage = NetworkStorageNode().build("", "/workspace", "preserve", 10, "")[0]

    with pytest.raises(ValidationError, match="Network storage requires"):
        Planner().build(DeployNode().build(agent, network_storage=bad_storage)[0])

    s3 = S3StorageNode().build("https://s3.example.test", "bucket", "us-east-1", "", "")[0]
    with pytest.raises(ValidationError, match="S3 storage requires"):
        Planner().build(DeployNode().build(agent, s3_storage=s3)[0])


def test_keep_alive_validation_rejects_incomplete_limits():
    for policy in (
        KeepAliveNode().build("time", "stop", 0, "seconds", 0, 0.0, 0)[0],
        KeepAliveNode().build("turns", "stop", 0, "seconds", 0, 0.0, 0)[0],
        KeepAliveNode().build("cost", "stop", 0, "seconds", 0, 0.0, 0)[0],
    ):
        try:
            validate_keep_alive(policy)
        except ValidationError as exc:
            assert "requires" in str(exc)
        else:
            raise AssertionError("expected ValidationError")

    invalid = KeepAliveNode().build("manual", "stop", 0, "seconds", 0, 0.0, 0)[0]
    invalid = invalid.__class__(**{**invalid.__dict__, "enforcement": "invalid"})
    with pytest.raises(ValidationError, match="Keep-alive enforcement"):
        validate_keep_alive(invalid)


def test_remote_sql_env_only_injects_database_url_from_server_env():
    spec = RemoteSQLDatabaseNode().build("Postgres", "env_only", "app", "app", database_url_env_var="APP_DATABASE_URL")[0]

    assert spec.materialization == "env_only"
    assert spec.template_key is None
    assert spec.runtime_contract.env.secrets[0].name == "APP_DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].env_var == "DATABASE_URL"
    assert spec.runtime_contract.env.secrets[0].provider == "server_env"


def test_remote_sql_own_pod_adds_client_command_and_database_skill():
    spec = RemoteSQLDatabaseNode().build("MySQL", "own_pod", "app", "app")[0]

    assert spec.runtime_contract.env.values["DATABASE_HOST"] == "crag://sql/mysql/host"
    assert spec.runtime_contract.env.values["DATABASE_URL"].startswith("mysql://app:app@crag://sql/mysql/hostport/app")
    assert spec.runtime_contract.commands[0].source == "database-client:mysql"
    assert "python3" in spec.runtime_contract.commands[0].command
    assert "default-mysql-client" in spec.runtime_contract.commands[0].command
    assert f"{CENTRAL_SKILLS_PATH}/crag-database/SKILL.md" in spec.runtime_contract.files
