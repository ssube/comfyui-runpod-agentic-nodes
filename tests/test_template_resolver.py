import pytest

from comfyui_runpod_agentic.template_resolver import TemplateResolutionError, TemplateResolver


def test_resolves_agent_capability_template():
    resolver = TemplateResolver()

    selection = resolver.resolve_agent("opencode", ["playwright"])

    assert selection.template_id == "rp-agent-opencode-playwright"


def test_missing_capability_fails_clearly():
    resolver = TemplateResolver()

    with pytest.raises(TemplateResolutionError, match="lacks required capabilities"):
        resolver.resolve_agent("pi", ["playwright"])
