import json
from pathlib import Path

import pytest

from comfyui_runpod_agentic.template_resolver import DEFAULT_TEMPLATE_CONFIG, TemplateResolutionError, TemplateResolver, is_unresolved_template_key


def test_resolves_agent_capability_template():
    resolver = TemplateResolver()

    selection = resolver.resolve_agent("opencode", ["playwright"])

    assert selection.template_id == "rp-agent-opencode-playwright"


def test_missing_capability_fails_clearly():
    resolver = TemplateResolver()

    with pytest.raises(TemplateResolutionError, match="lacks required capabilities"):
        resolver.resolve_agent("pi", ["playwright"])


def test_bootstrap_templates_cover_default_template_keys():
    bootstrap = json.loads(Path("defaults/runpod_templates.bootstrap.json").read_text())
    bootstrap_keys = {template["key"] for template in bootstrap["templates"]}
    expected = set()
    for entry in DEFAULT_TEMPLATE_CONFIG["agent_templates"].values():
        expected.add(entry["default"])
        expected.update(entry.get("capabilities", {}).values())
    for kinds in DEFAULT_TEMPLATE_CONFIG["app_templates"].values():
        expected.update(kinds.values())

    assert expected.difference(bootstrap_keys) == set()


def test_unresolved_template_key_detects_symbolic_defaults():
    assert is_unresolved_template_key("rp-agent-codex")
    assert not is_unresolved_template_key("x5833iirmd")
