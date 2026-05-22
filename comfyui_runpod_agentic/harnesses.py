from __future__ import annotations

from dataclasses import dataclass

CENTRAL_SKILLS_PATH = "/workspace/.runpod_agentic/skills"


@dataclass(frozen=True)
class HarnessSupport:
    harness: str
    display_name: str
    binary: str
    prompt: bool
    model: bool
    system_prompt: bool
    llm_env: bool
    mcp_env: bool
    skills_symlink: bool
    response_capture: bool


HARNESS_SUPPORT = {
    "codex": HarnessSupport("codex", "Codex", "codex", True, True, False, True, True, True, True),
    "claude": HarnessSupport("claude", "Claude", "claude", True, True, True, True, True, True, True),
    "opencode": HarnessSupport("opencode", "OpenCode", "opencode", True, True, False, True, True, True, True),
    "hermes": HarnessSupport("hermes", "Hermes", "hermes", True, True, False, True, True, True, True),
    "pi": HarnessSupport("pi", "Pi", "pi", True, True, True, True, True, True, True),
}


def harness_matrix_rows() -> list[dict[str, str | bool]]:
    return [
        {
            "harness": item.display_name,
            "binary": item.binary,
            "prompt": item.prompt,
            "model": item.model,
            "system_prompt": item.system_prompt,
            "llm_env": item.llm_env,
            "mcp_env": item.mcp_env,
            "skills_symlink": item.skills_symlink,
            "response_capture": item.response_capture,
        }
        for item in HARNESS_SUPPORT.values()
    ]
