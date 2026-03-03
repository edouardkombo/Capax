from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Literal, List

import yaml

CombineMode = Literal["choice_max", "bundle_sum"]
UnknownTokenAction = Literal["default_cost", "reject_400"]


@dataclass
class HttpCodes:
    accepted: int = 200
    at_capacity: int = 429


@dataclass
class CostExpression:
    field: Optional[str] = None
    separators: List[str] = None
    combine_mode: CombineMode = "choice_max"
    token_costs: Dict[str, int] = None
    unknown_action: UnknownTokenAction = "default_cost"
    unknown_default_cost: int = 1

    def __post_init__(self) -> None:
        if self.separators is None:
            self.separators = ["|", " OR ", " or "]
        if self.token_costs is None:
            self.token_costs = {}


@dataclass
class Isolation:
    enabled: bool = False
    field: Optional[str] = None


@dataclass
class Idempotency:
    enabled: bool = False
    ttl_seconds: int = 600


@dataclass
class Policy:
    pack_name: str
    endpoint_method: str
    endpoint_path: str
    status_path: str = "/status"
    max_allowed_concurrent_capacity: int = 2
    duration_seconds: float = 5.0
    cost: CostExpression = None
    isolation: Isolation = None
    http: HttpCodes = None
    idempotency: Idempotency = None

    def __post_init__(self) -> None:
        if self.cost is None:
            self.cost = CostExpression()
        if self.isolation is None:
            self.isolation = Isolation()
        if self.http is None:
            self.http = HttpCodes()
        if self.idempotency is None:
            self.idempotency = Idempotency()


def load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def policy_to_dict(p: Policy) -> Dict[str, Any]:
    return {
        "pack_name": p.pack_name,
        "http": {
            "endpoint": {"method": p.endpoint_method.upper(), "path": p.endpoint_path},
            "status_path": p.status_path,
            "codes": {"accepted": p.http.accepted, "at_capacity": p.http.at_capacity},
        },
        "capacity": {
            "max_allowed_concurrent_capacity": int(p.max_allowed_concurrent_capacity),
            "duration_seconds": float(p.duration_seconds),
            "isolation": {"enabled": bool(p.isolation.enabled), "field": p.isolation.field},
        },
        "cost_expression": {
            "field": p.cost.field,
            "separators": list(p.cost.separators),
            "combine_mode": p.cost.combine_mode,
            "token_costs": {str(k): int(v) for k, v in p.cost.token_costs.items()},
            "unknown": {"action": p.cost.unknown_action, "default_cost": int(p.cost.unknown_default_cost)},
            # IMPORTANT: runtime SHOULD treat tokens case-insensitively.
            "case_insensitive": True,
        },
        "idempotency": {"enabled": bool(p.idempotency.enabled), "ttl_seconds": int(p.idempotency.ttl_seconds)},
    }


def policy_from_dict(d: Dict[str, Any]) -> Policy:
    http = d.get("http", {})
    endpoint = http.get("endpoint", {})
    codes = http.get("codes", {})
    cap = d.get("capacity", {})
    iso = cap.get("isolation", {}) or {}
    cost = d.get("cost_expression", {}) or {}
    unknown = cost.get("unknown", {}) or {}
    idem = d.get("idempotency", {}) or {}

    return Policy(
        pack_name=d["pack_name"],
        endpoint_method=endpoint.get("method", "POST"),
        endpoint_path=endpoint.get("path", "/order"),
        status_path=http.get("status_path", "/status"),
        max_allowed_concurrent_capacity=int(cap.get("max_allowed_concurrent_capacity", 2)),
        duration_seconds=float(cap.get("duration_seconds", 5.0)),
        cost=CostExpression(
            field=cost.get("field"),
            separators=cost.get("separators") or ["|", " OR ", " or "],
            combine_mode=cost.get("combine_mode", "choice_max"),
            token_costs=cost.get("token_costs") or {},
            unknown_action=unknown.get("action", "default_cost"),
            unknown_default_cost=int(unknown.get("default_cost", 1)),
        ),
        isolation=Isolation(enabled=bool(iso.get("enabled", False)), field=iso.get("field")),
        http=HttpCodes(accepted=int(codes.get("accepted", 200)), at_capacity=int(codes.get("at_capacity", 429))),
        idempotency=Idempotency(enabled=bool(idem.get("enabled", False)), ttl_seconds=int(idem.get("ttl_seconds", 600))),
    )


def load_policy_yaml(policy_yaml: Path) -> Policy:
    return policy_from_dict(load_yaml(policy_yaml))


def compile_policy(policy_yaml: Path, out_json: Path) -> None:
    p = load_policy_yaml(policy_yaml)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(policy_to_dict(p), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
