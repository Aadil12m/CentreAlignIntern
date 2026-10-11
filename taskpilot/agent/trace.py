"""Append-only JSONL trace of everything that happened in a run.

The trace is the audit log: every model turn, tool call, tool result,
approval and verification verdict, in order. Secrets never reach it because
tools only ever see secret placeholders.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Trace:
    def __init__(self, path: Path, echo: bool = False):
        self.path = path
        self.echo = echo
        self.t0 = time.time()
        self.events: list[dict] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a")

    def log(self, kind: str, **data) -> None:
        event = {"t": round(time.time() - self.t0, 2), "kind": kind, **data}
        self.events.append(event)
        self._f.write(json.dumps(event, default=str) + "\n")
        self._f.flush()
        if self.echo:
            print(_pretty(event), flush=True)

    def close(self) -> None:
        self._f.close()


def _pretty(e: dict) -> str:
    k = e["kind"]
    if k == "tool_call":
        args = json.dumps(e["input"], ensure_ascii=False)
        return f"  -> {e['tool']}({args[:160]})"
    if k == "tool_result":
        first = e["output"].splitlines()[0] if e["output"] else ""
        return f"     {'ok ' if e['ok'] else 'ERR'} {first[:150]}"
    if k == "thought":
        return f"  ~ {e['text'][:300]}"
    if k in {"phase", "verdict", "approval", "human", "guard", "plan", "retry"}:
        body = {x: y for x, y in e.items() if x not in {"t", "kind"}}
        return f"[{k}] {json.dumps(body, ensure_ascii=False, default=str)[:400]}"
    return f"[{k}]"
