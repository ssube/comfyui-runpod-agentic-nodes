from __future__ import annotations

import re
from dataclasses import replace

try:
    from .specs import EnvPatch, RuntimeContract, SecretRef
except ImportError:
    from specs import EnvPatch, RuntimeContract, SecretRef

SECRET_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def merge_contracts(*contracts: RuntimeContract | None) -> RuntimeContract:
    values: dict[str, str] = {}
    secrets: list[SecretRef] = []
    ports = []
    files: dict[str, str] = {}
    commands = []
    for contract in contracts:
        if contract is None:
            continue
        values.update(contract.env.values)
        secrets.extend(contract.env.secrets)
        ports.extend(contract.ports)
        files.update(contract.files)
        commands.extend(contract.commands)
    return RuntimeContract(env=EnvPatch(values, secrets), ports=ports, files=files, commands=commands)


def with_env(contract: RuntimeContract, values: dict[str, str]) -> RuntimeContract:
    merged = dict(contract.env.values)
    merged.update(values)
    return replace(contract, env=EnvPatch(merged, list(contract.env.secrets)))


def redact_env(values: dict[str, str]) -> dict[str, str]:
    return {key: ("<redacted>" if SECRET_PATTERN.search(key) else value) for key, value in values.items()}


def secret_placeholder(secret: SecretRef) -> str:
    if secret.provider == "server_env":
        return f"${{{secret.name}}}"
    if secret.provider == "literal_for_dev_only":
        return "<literal-dev-secret-redacted>"
    return f"{{{{ RUNPOD_SECRET_{secret.name} }}}}"
