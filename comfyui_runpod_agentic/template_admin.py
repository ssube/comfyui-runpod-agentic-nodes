from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runpod_client import RunpodClient


def save_templates(spec_path: Path, map_path: Path, *, client: RunpodClient | None = None, dry_run: bool = False) -> dict[str, str]:
    client = client or RunpodClient()
    specs = json.loads(spec_path.read_text())
    existing = load_id_map(map_path)
    result = dict(existing)
    templates = specs if isinstance(specs, list) else specs.get("templates", [])
    if not isinstance(templates, list):
        raise ValueError("Template spec JSON must be a list or an object with a templates list.")

    for template in templates:
        key = template_key(template)
        payload = dict(template)
        payload.pop("key", None)
        payload.setdefault("env", [])
        payload.setdefault("dockerArgs", "")
        if key in existing and "id" not in payload:
            payload["id"] = existing[key]
        if dry_run:
            print(json.dumps({"key": key, "input": redact_template_payload(payload)}, sort_keys=True))
            continue
        saved = client.save_template(payload)
        result[key] = saved["id"]
        write_id_map(map_path, result)
        print(json.dumps({"key": key, "id": saved["id"], "name": saved["name"]}, sort_keys=True))
    return result


def template_key(template: dict[str, Any]) -> str:
    key = template.get("key") or template.get("name")
    if not key:
        raise ValueError("Each template must include key or name.")
    return str(key)


def load_id_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Template ID map must be a JSON object.")
    return {str(key): str(value) for key, value in data.items()}


def write_id_map(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def redact_template_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    env = redacted.get("env")
    if isinstance(env, list):
        redacted["env"] = [
            {**item, "value": "<redacted>"}
            if isinstance(item, dict) and any(token in str(item.get("key", "")).upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            else item
            for item in env
        ]
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update Runpod templates and persist their IDs.")
    parser.add_argument("--spec", default="defaults/runpod_templates.bootstrap.json", help="JSON template spec file.")
    parser.add_argument("--map", default="defaults/runpod_template_ids.json", help="Output JSON map of template key/name to Runpod template ID.")
    parser.add_argument("--dry-run", action="store_true", help="Print sanitized mutation inputs without calling Runpod.")
    args = parser.parse_args()
    save_templates(Path(args.spec), Path(args.map), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
