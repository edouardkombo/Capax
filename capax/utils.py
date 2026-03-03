from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List


def now_ms() -> int:
    return int(time.time() * 1000)


def stable_json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint_request(method: str, path: str, body: Dict[str, Any]) -> str:
    payload = f"{method.upper()} {path}\n{stable_json_dumps(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def infer_scalar_type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int) or isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if v is None:
        return "null"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return "unknown"


@dataclass(frozen=True)
class FieldInfo:
    name: str
    type: str


def infer_fields(sample: Dict[str, Any]) -> List[FieldInfo]:
    return [FieldInfo(name=k, type=infer_scalar_type(v)) for k, v in sample.items()]


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
