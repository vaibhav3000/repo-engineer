"""The audit trace: one JSON object per event, streamed as the run happens.

Every event carries a sequence number, the milliseconds since the trace
started, and the runtime state at that moment (the sink tracks it from the
"state" events, so emitters never pass it). TraceWriter appends each event
to a .jsonl file and flushes it, so a run that crashes still leaves its
history behind.

Event kinds (see docs/design/step2.md):
  run_start         task, configuration, sandbox, planner, starting workspace digest
  state             a state transition (from, to)
  planner_decision  what the planner returned: a call, done, or an error
  model_call        one provider call: latency, tokens, cost, stop reason, tool calls
  compaction        tool results stubbed out to fit the context budget
  tool_call         arguments, the observation and latency of one executed call
  verify            the verification verdict
  run_end           final state and totals

Costs come only from a price table the user supplies; none are built in.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol

from .models import Usage
from .snapshot import SKIPPED_DIRS, list_files


class EventSink(Protocol):
    """Receives audit events."""

    def emit(self, kind: str, **fields: Any) -> None: ...


class _Clocked:
    """Numbering, timing and state tracking shared by the sinks."""

    def __init__(self) -> None:
        self._seq = 0
        self._started = time.perf_counter()
        self.state = "INIT"

    def _event(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        if kind == "state" and "to" in fields:
            self.state = str(fields["to"])
        event = {
            "seq": self._seq,
            "kind": kind,
            "t_ms": round((time.perf_counter() - self._started) * 1000, 1),
            "state": self.state,
            **fields,
        }
        self._seq += 1
        return event


class NullSink:
    def emit(self, kind: str, **fields: Any) -> None:
        return None


class MemorySink(_Clocked):
    """Keeps events in a list (tests, replay comparisons)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append(self._event(kind, fields))


class TraceWriter(_Clocked):
    """Appends events to a JSON Lines file, flushing after each one."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: IO[str] | None = self.path.open("a", encoding="utf-8", newline="\n")

    def emit(self, kind: str, **fields: Any) -> None:
        if self._handle is None:
            raise ValueError(f"trace {self.path} is closed")
        self._handle.write(json.dumps(self._event(kind, fields), default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_trace(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every event in a trace file, in order."""
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def workspace_digest(root: Path) -> str:
    """SHA-256 over the workspace's regular files (paths and contents).

    Caches and .git are skipped, so the digest identifies the code a run
    started from, not the machine it ran on.
    """
    # Resolved first: under a short (8.3) or linked spelling of the root,
    # every file would look like an alias and be skipped.
    root = root.resolve()
    digest = hashlib.sha256()
    for rel in sorted(list_files(root, skip=SKIPPED_DIRS)):
        if rel.endswith(".pyc"):
            continue
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None


class PriceTable:
    """USD per million tokens by model, loaded from a user-supplied JSON file.

    {"model-id": {"input_per_mtok": 1.25, "output_per_mtok": 10.0,
                  "cache_read_per_mtok": 0.125, "cache_write_per_mtok": 1.5}}

    Cached input is charged at its own rate when one is given and at the
    input rate otherwise. A model without an entry has no cost (None).
    """

    def __init__(self, prices: dict[str, Price]) -> None:
        self.prices = prices

    @classmethod
    def empty(cls) -> PriceTable:
        return cls({})

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> PriceTable:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls({model: Price(**entry) for model, entry in raw.items()})

    def cost(self, model: str, usage: Usage) -> float | None:
        price = self.prices.get(model)
        if price is None:
            return None
        cache_read_rate = price.cache_read_per_mtok
        cache_write_rate = price.cache_write_per_mtok
        cached = usage.cache_read_tokens if cache_read_rate is not None else 0
        written = usage.cache_write_tokens if cache_write_rate is not None else 0
        uncached = usage.input_tokens - cached - written
        total = uncached * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
        total += cached * (cache_read_rate or 0.0) + written * (cache_write_rate or 0.0)
        return total / 1_000_000


def jsonable(value: Any) -> Any:
    """value if it serialises to JSON, else its string form (for tool arguments)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def total_cost(costs: Iterable[float | None]) -> float | None:
    """The sum of known costs, or None when none was known."""
    known = [c for c in costs if c is not None]
    return round(sum(known), 6) if known else None
