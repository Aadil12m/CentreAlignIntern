"""Two kinds of memory.

* Working memory: facts discovered during this run (invoice number, amount,
  the ERP id that was created). It is shown back to the model whenever it
  changes, handed to the verifier, and saved in the run report.
* Long-term memory: short lessons learned from previous runs ("the ERP wants
  dates as DD/MM/YYYY"), stored as JSON lines and loaded into later runs so
  the agent stops repeating the same mistakes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class WorkingMemory:
    def __init__(self):
        self.facts: dict[str, str] = {}

    def remember(self, key: str, value: str) -> None:
        self.facts[key] = value

    def render(self) -> str:
        if not self.facts:
            return "(empty)"
        return "\n".join(f"- {k}: {v}" for k, v in self.facts.items())


class LongTermMemory:
    def __init__(self, path: str | Path | None, limit: int = 30):
        self.path = Path(path) if path else None
        self.limit = limit

    def lessons(self) -> list[dict]:
        if not self.path or not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        return rows[-self.limit:]

    def render(self) -> str:
        rows = self.lessons()
        return "\n".join(f"- [{r.get('scope', 'general')}] {r['lesson']}" for r in rows) or "(none yet)"

    def add(self, lessons: list[dict], source_run: str) -> None:
        if not self.path or not lessons:
            return
        existing = {r["lesson"].strip().lower() for r in self.lessons()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            for item in lessons:
                if item["lesson"].strip().lower() in existing:
                    continue
                f.write(json.dumps({**item, "run": source_run, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
