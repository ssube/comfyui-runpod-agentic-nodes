from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RunpodAuthConfig:
    api_key_env: str = "RUNPOD_API_KEY"


def get_runpod_api_key(config: RunpodAuthConfig | None = None) -> str | None:
    auth = config or RunpodAuthConfig()
    return os.environ.get(auth.api_key_env)
