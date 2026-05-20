from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


class TemplateResolutionError(ValueError):
    pass


DEFAULT_TEMPLATE_CONFIG: dict[str, Any] = {
    "agent_templates": {
        "codex": {"default": "rp-agent-codex", "capabilities": {"playwright": "rp-agent-codex-playwright"}},
        "claude": {"default": "rp-agent-claude", "capabilities": {"playwright": "rp-agent-claude-playwright"}},
        "opencode": {"default": "rp-agent-opencode", "capabilities": {"playwright": "rp-agent-opencode-playwright"}},
        "hermes": {"default": "rp-agent-hermes", "capabilities": {}},
        "pi": {"default": "rp-agent-pi", "capabilities": {}},
    },
    "app_templates": {
        "browser": {"playwright": "rp-browser-playwright", "neko": "rp-browser-neko"},
        "llm_server": {"ollama": "rp-llm-ollama", "vllm": "rp-llm-vllm"},
        "sql_database": {"postgres": "rp-db-postgres", "mysql": "rp-db-mysql"},
        "vector_database": {"chroma": "rp-vector-chroma", "qdrant": "rp-vector-qdrant"},
    },
}


@dataclass(frozen=True)
class TemplateSelection:
    template_id: str
    image_name: str | None = None
    ports: list[dict[str, Any]] = field(default_factory=list)
    startup_command: str | None = None


class TemplateResolver:
    def __init__(self, config: dict[str, Any] | None = None, template_ids: dict[str, str] | None = None):
        self.config = config or DEFAULT_TEMPLATE_CONFIG
        self.template_ids = template_ids if template_ids is not None else load_template_id_map()

    @classmethod
    def from_file(cls, path: str | Path) -> TemplateResolver:
        if yaml is None:
            raise TemplateResolutionError("PyYAML is required to read template YAML files.")
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls(data)

    def resolve_agent(self, harness: str, capabilities: list[str] | None = None) -> TemplateSelection:
        templates = self.config.get("agent_templates", {})
        entry = templates.get(harness)
        if not entry:
            raise TemplateResolutionError(f"No agent template configured for harness {harness}.")
        capabilities = capabilities or []
        if not capabilities:
            return TemplateSelection(template_id=self._resolve_id(entry["default"]))
        capability_templates = entry.get("capabilities", {})
        missing = [capability for capability in capabilities if capability not in capability_templates]
        if missing:
            raise TemplateResolutionError(
                f"Agent template {harness} lacks required capabilities: {', '.join(missing)}."
            )
        if len(capabilities) > 1:
            raise TemplateResolutionError("Multiple same-pod capabilities require an explicit combined template.")
        return TemplateSelection(template_id=self._resolve_id(capability_templates[capabilities[0]]))

    def resolve_app(self, kind: str, engine: str) -> TemplateSelection:
        template_id = self.config.get("app_templates", {}).get(kind, {}).get(engine)
        if not template_id:
            raise TemplateResolutionError(f"No template configured for {kind}/{engine}.")
        return TemplateSelection(template_id=self._resolve_id(template_id))

    def _resolve_id(self, key_or_id: str) -> str:
        return self.template_ids.get(key_or_id, key_or_id)


def load_template_id_map(path: str | Path = "defaults/runpod_template_ids.json") -> dict[str, str]:
    candidate = Path(path)
    if not candidate.exists():
        candidate = Path(__file__).resolve().parents[1] / path
    if not candidate.exists():
        return {}
    data = json.loads(candidate.read_text())
    if not isinstance(data, dict):
        raise TemplateResolutionError(f"Template ID map must be a JSON object: {candidate}")
    return {str(key): str(value) for key, value in data.items()}


def is_unresolved_template_key(template_id: str | None) -> bool:
    return bool(template_id and template_id.startswith("rp-"))
