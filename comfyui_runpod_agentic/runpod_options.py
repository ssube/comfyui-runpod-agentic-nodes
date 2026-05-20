from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from .config import get_runpod_api_key
from .runpod_client import RunpodClient


@dataclass(frozen=True)
class RunpodDropdownOptions:
    gpu_type_ids: list[str]
    data_center_ids: list[str]


_CACHE: tuple[float, RunpodDropdownOptions] | None = None


def runpod_dropdown_options() -> RunpodDropdownOptions:
    global _CACHE
    ttl = int(os.environ.get("CRAG_RUNPOD_OPTIONS_TTL_SECONDS", "300"))
    now = time.monotonic()
    if _CACHE and now - _CACHE[0] < ttl:
        return _CACHE[1]
    options = fetch_runpod_dropdown_options()
    _CACHE = (now, options)
    return options


def fetch_runpod_dropdown_options() -> RunpodDropdownOptions:
    api_key = get_runpod_api_key()
    if not api_key:
        return RunpodDropdownOptions([], [])
    client = RunpodClient(api_key=api_key, timeout_seconds=int(os.environ.get("CRAG_RUNPOD_OPTIONS_TIMEOUT_SECONDS", "8")))
    try:
        gpu_types = client.list_gpu_types()
        datacenters = client.list_datacenters()
    except Exception:
        return RunpodDropdownOptions([], [])
    if not datacenters:
        datacenters = datacenters_from_gpu_types(gpu_types)
    return RunpodDropdownOptions(
        gpu_type_ids=sorted_unique_ids(gpu_types),
        data_center_ids=sorted_unique_ids(item for item in datacenters if item.get("listed") is not False),
    )


def datacenters_from_gpu_types(gpu_types: Any) -> list[dict[str, Any]]:
    datacenters = []
    for gpu_type in gpu_types or []:
        if isinstance(gpu_type, dict):
            datacenters.extend(item for item in gpu_type.get("nodeGroupDatacenters") or [] if isinstance(item, dict))
    return datacenters


def sorted_unique_ids(items: Any) -> list[str]:
    values = []
    for item in items or []:
        value = item.get("id") if isinstance(item, dict) else None
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return sorted(set(values), key=str.casefold)


def optional_combo_or_string(values: list[str], default: str = ""):
    if values:
        choices = [default, *values]
        return (choices,)
    return ("STRING", {"default": default})
