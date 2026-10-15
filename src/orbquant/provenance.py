"""Canonical serialization and content-addressed IDs for replayable artifacts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def to_primitive(value: Any) -> Any:
    """Convert supported immutable values into a stable JSON-compatible tree."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_primitive(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be serialized into provenance")
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot be serialized")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("provenance mapping keys must be strings")
        return {key: to_primitive(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        primitives = [to_primitive(item) for item in value]
        return sorted(primitives, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    if hasattr(value, "item"):
        # NumPy scalar support without taking a hard dependency in this module.
        return to_primitive(value.item())
    raise TypeError(f"unsupported provenance value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize values with stable key ordering and no whitespace variance."""
    return json.dumps(to_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_hash(value: Any) -> str:
    """Return a lowercase SHA-256 digest of canonical JSON or raw bytes."""
    payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_id(kind: str, value: Any, *, prefix_length: int = 20) -> str:
    """Return a readable, deterministic ID such as ``config:abc123``."""
    if not kind or any(character.isspace() for character in kind):
        raise ValueError("kind must be a non-empty token without whitespace")
    if prefix_length < 8 or prefix_length > 64:
        raise ValueError("prefix_length must be between 8 and 64")
    return f"{kind}:{content_hash(value)[:prefix_length]}"


def hash_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Explicitly hash a file in chunks; callers decide when filesystem access is allowed."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
