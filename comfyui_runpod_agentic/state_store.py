from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  prompt_id TEXT,
  workflow_hash TEXT,
  created_at TEXT,
  updated_at TEXT,
  mode TEXT,
  status TEXT,
  deployment_hash TEXT
);
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  node_id TEXT,
  role TEXT,
  desired_hash TEXT,
  runpod_pod_id TEXT,
  runpod_template_id TEXT,
  name TEXT,
  status TEXT,
  cost_per_hr REAL,
  created_at TEXT,
  last_seen_at TEXT,
  stop_after TEXT,
  terminate_after TEXT
);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  phase TEXT,
  order_index INTEGER,
  command_hash TEXT,
  status TEXT,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  stdout_path TEXT,
  stderr_path TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  timestamp TEXT,
  event_type TEXT,
  message TEXT,
  payload_json TEXT
);
CREATE TABLE IF NOT EXISTS counters (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  resource_id TEXT,
  counter_type TEXT,
  value REAL,
  updated_at TEXT
);
"""


@dataclass
class StateStore:
    path: str | Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_run(self, run_id: str, workflow_hash: str, deployment_hash: str, mode: str, status: str, prompt_id: str | None = None) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, prompt_id, workflow_hash, created_at, updated_at, mode, status, deployment_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, mode=excluded.mode, status=excluded.status
                """,
                (run_id, prompt_id, workflow_hash, now, now, mode, status, deployment_hash),
            )

    def record_resource(self, run_id: str, resource: Any, pod: dict[str, Any] | None = None, status: str = "planned") -> str:
        now = utc_now()
        resource_id = f"{run_id}:{resource.role}:{resource.desired_hash}"
        pod = pod or {}
        cost = pod.get("adjustedCostPerHr") or pod.get("costPerHr")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO resources (id, run_id, node_id, role, desired_hash, runpod_pod_id, runpod_template_id, name, status, cost_per_hr, created_at, last_seen_at, stop_after, terminate_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET runpod_pod_id=excluded.runpod_pod_id, status=excluded.status, cost_per_hr=excluded.cost_per_hr, last_seen_at=excluded.last_seen_at
                """,
                (
                    resource_id,
                    run_id,
                    resource.node_id,
                    resource.role,
                    resource.desired_hash,
                    pod.get("id"),
                    resource.template_id,
                    resource.name,
                    status,
                    cost,
                    now,
                    now,
                    resource.pod_input.get("stopAfter"),
                    resource.pod_input.get("terminateAfter"),
                ),
            )
        return resource_id

    def record_remote_resource(self, pod: dict[str, Any], *, run_id: str | None = None) -> str:
        name = str(pod.get("name") or "")
        parsed = parse_managed_name(name)
        resource_id = pod.get("id") or name
        now = utc_now()
        cost = pod.get("adjustedCostPerHr") or pod.get("costPerHr")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO resources (id, run_id, node_id, role, desired_hash, runpod_pod_id, runpod_template_id, name, status, cost_per_hr, created_at, last_seen_at, stop_after, terminate_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET runpod_pod_id=excluded.runpod_pod_id, status=excluded.status, cost_per_hr=excluded.cost_per_hr, last_seen_at=excluded.last_seen_at
                """,
                (
                    resource_id,
                    run_id or pod_env(pod).get("CRAG_RUN_ID"),
                    pod_env(pod).get("CRAG_NODE_ID") or parsed.get("node_id"),
                    pod_env(pod).get("CRAG_ROLE") or parsed.get("role"),
                    pod_env(pod).get("CRAG_DESIRED_HASH") or parsed.get("desired_hash"),
                    pod.get("id"),
                    pod.get("templateId") or pod.get("template_id"),
                    name,
                    pod.get("desiredStatus") or pod.get("status"),
                    cost,
                    now,
                    now,
                    None,
                    None,
                ),
            )
        return str(resource_id)

    def start_command(self, run_id: str, resource_id: str | None, phase: str, order_index: int, command_hash: str, stdout_path: str, stderr_path: str) -> str:
        command_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO commands (id, run_id, resource_id, phase, order_index, command_hash, status, started_at, stdout_path, stderr_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (command_id, run_id, resource_id, phase, order_index, command_hash, "running", utc_now(), stdout_path, stderr_path),
            )
        return command_id

    def finish_command(self, command_id: str, status: str, exit_code: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE commands SET status = ?, finished_at = ?, exit_code = ? WHERE id = ?",
                (status, utc_now(), exit_code, command_id),
            )

    def add_event(self, run_id: str, event_type: str, message: str, *, resource_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events (id, run_id, resource_id, timestamp, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, run_id, resource_id, utc_now(), event_type, message, json.dumps(payload or {}, sort_keys=True)),
            )

    def increment_counter(self, run_id: str, counter_type: str, amount: float = 1, resource_id: str | None = None) -> float:
        counter_id = f"{run_id}:{resource_id or 'run'}:{counter_type}"
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM counters WHERE id = ?", (counter_id,)).fetchone()
            value = float(row["value"] if row else 0) + amount
            conn.execute(
                """
                INSERT INTO counters (id, run_id, resource_id, counter_type, value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (counter_id, run_id, resource_id, counter_type, value, utc_now()),
            )
        return value

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_resources(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM resources ORDER BY created_at DESC").fetchall()]

    def list_commands(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM commands WHERE run_id = ? ORDER BY started_at", (run_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM commands ORDER BY started_at DESC").fetchall()
            return [dict(row) for row in rows]

    def list_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY timestamp", (run_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY timestamp DESC").fetchall()
            return [dict(row) for row in rows]

    def mark_resource_status(self, resource_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE resources SET status = ?, last_seen_at = ? WHERE id = ?", (status, utc_now(), resource_id))


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def pod_env(pod: dict[str, Any]) -> dict[str, str]:
    env = pod.get("env") or pod.get("machine", {}).get("env") or []
    if isinstance(env, dict):
        return {str(key): str(value) for key, value in env.items()}
    if isinstance(env, list):
        values: dict[str, str] = {}
        for item in env:
            if isinstance(item, dict):
                key = item.get("key") or item.get("name")
                value = item.get("value")
                if key and value is not None:
                    values[str(key)] = str(value)
        return values
    return {}


def parse_managed_name(name: str) -> dict[str, str]:
    parts = name.split("-")
    if len(parts) < 5 or parts[0] != "crag":
        return {}
    return {"workflow_hash": parts[1], "role": parts[2], "node_id": parts[3], "desired_hash": parts[4]}
