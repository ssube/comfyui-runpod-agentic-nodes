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


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
