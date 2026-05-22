import json
from pathlib import Path

import pytest

from comfyui_runpod_agentic.template_resolver import (
    DEFAULT_TEMPLATE_CONFIG,
    TemplateResolutionError,
    TemplateResolver,
    is_unresolved_template_key,
    load_template_id_map,
)


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


def test_template_resolver_loads_yaml_config_and_resolves_ids(tmp_path):
    config = tmp_path / "templates.yaml"
    config.write_text(
        """
agent_templates:
  custom:
    default: rp-agent-custom
    capabilities:
      playwright: rp-agent-custom-playwright
app_templates:
  browser:
    custom: rp-browser-custom
"""
    )
    resolver = TemplateResolver.from_file(config)
    resolver.template_ids = {"rp-agent-custom": "agent-id", "rp-browser-custom": "browser-id"}

    assert resolver.resolve_agent("custom").template_id == "agent-id"
    assert resolver.resolve_app("browser", "custom").template_id == "browser-id"


def test_template_resolver_reports_missing_and_ambiguous_templates():
    resolver = TemplateResolver(template_ids={})

    with pytest.raises(TemplateResolutionError, match="No agent template"):
        resolver.resolve_agent("missing")
    with pytest.raises(TemplateResolutionError, match="No template configured"):
        resolver.resolve_app("browser", "missing")
    combined = TemplateResolver(config={"agent_templates": {"custom": {"default": "agent", "capabilities": {"playwright": "one", "gpu": "two"}}}}, template_ids={})
    with pytest.raises(TemplateResolutionError, match="Multiple same-pod"):
        combined.resolve_agent("custom", ["playwright", "gpu"])


def test_load_template_id_map_handles_missing_and_invalid_files(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]")

    assert load_template_id_map(missing) == {}
    with pytest.raises(TemplateResolutionError, match="JSON object"):
        load_template_id_map(invalid)
