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
    "codex": HarnessSupport(harness="codex", display_name="Codex", binary="codex", prompt=True, model=True, system_prompt=False, llm_env=True, mcp_env=True, skills_symlink=True, response_capture=True),
    "claude": HarnessSupport(harness="claude", display_name="Claude", binary="claude", prompt=True, model=True, system_prompt=True, llm_env=True, mcp_env=True, skills_symlink=True, response_capture=True),
    "opencode": HarnessSupport(harness="opencode", display_name="OpenCode", binary="opencode", prompt=True, model=True, system_prompt=False, llm_env=True, mcp_env=True, skills_symlink=True, response_capture=True),
    "hermes": HarnessSupport(harness="hermes", display_name="Hermes", binary="hermes", prompt=True, model=True, system_prompt=False, llm_env=True, mcp_env=True, skills_symlink=True, response_capture=True),
    "pi": HarnessSupport(harness="pi", display_name="Pi", binary="pi", prompt=True, model=True, system_prompt=True, llm_env=True, mcp_env=True, skills_symlink=True, response_capture=True),
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
