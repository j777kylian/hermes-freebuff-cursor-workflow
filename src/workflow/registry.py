"""Durable task registry.

Maps ``task_id`` to project metadata so that short-lived processes such as the
Discord ``/devdecide`` handler can locate a running task without holding any
agent conversation state. Stored outside Git under the workflow runtime dir.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import config


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = path or config.registry_path()
        self.data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self.data = {str(k): dict(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError):
                self.data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def put(self, task_id: str, **fields: Any) -> None:
        entry = dict(self.data.get(task_id, {}))
        entry["task_id"] = task_id
        entry.update(fields)
        entry["updated_at"] = utc_now()
        entry.setdefault("created_at", utc_now())
        self.data[task_id] = entry
        self._save()

    def get(self, task_id: str) -> dict[str, Any] | None:
        entry = self.data.get(task_id)
        return dict(entry) if entry else None

    def find_by_alias(self, alias: str) -> list[dict[str, Any]]:
        return [
            dict(e)
            for e in self.data.values()
            if e.get("alias") == alias
        ]

    def remove(self, task_id: str) -> None:
        if task_id in self.data:
            del self.data[task_id]
            self._save()

    def all(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.data.values()]
