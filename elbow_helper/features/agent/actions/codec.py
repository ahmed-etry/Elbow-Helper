"""Bound JSON records written by action and scheduling stores."""

from __future__ import annotations

import json
from typing import Any


def encode_record(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > 100_000:
        raise ValueError("Action record is too large")
    return encoded
