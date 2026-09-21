"""JSON output for the two agent-facing CLIs.

`client/cli.py` and `orchestrator/cli.py` are siblings — neither imports
the other — and both must serialize the same return types: dataclasses,
pydantic records, `Path`, enums, tuples. The helper lives here because
`gate` is the layer both already depend on.

The rule these CLIs follow, and the reason `--json` exists at all: **an
outcome belongs in the payload, not in the exit code.** A lost claim race
is a routine answer ("someone got there first, take another task"), and an
exit code cannot say that without also saying "something went wrong".
"""

from __future__ import annotations

import dataclasses
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any


def plain(o: Any) -> Any:
    """Anything the toolkit returns, as JSON-safe data."""
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, Enum):
        return o.name
    if isinstance(o, Path):
        return str(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: plain(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if hasattr(o, "model_dump"):          # pydantic (TaskRecord)
        return plain(o.model_dump(mode="json"))
    if isinstance(o, dict):
        return {str(k): plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [plain(v) for v in o]
    return str(o)


def emit(obj: Any) -> int:
    """Write `obj` as JSON to stdout. Always returns 0 — the caller's
    outcome is inside the payload, not in this return value."""
    json.dump(plain(obj), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
