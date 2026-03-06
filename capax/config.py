from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml

CombineMode = Literal["choice_max", "bundle_sum"]
UnknownTokenAction = Literal["default_cost", "reject_400"]
FailMode = Literal["closed", "open"]


@dataclass
class HttpCodes:
    accepted: int = 200
    at_capacity: int = 429
    bad_request: int = 400


@dataclass
class CostExpression:
    field: Optional[str] = None
    separators: List[str] = dc_field(default_factory=lambda: ["|", " OR ", " or "])
    combine_mode: CombineMode = "choice_max"
    token_costs: Dict[str, int] = dc_field(default_factory=dict)
    unknown_action: UnknownTokenAction = "default_cost"
    unknown_default_cost: int = 1
    missing_field_action: UnknownTokenAction = "default_cost"
    missing_field_default_cost: int = 1
    case_insensitive: bool = True


@dataclass
class Isolation:
    enabled: bool = False
    field: Optional[str] = None


@dataclass
class Idempotency:
    enabled: bool = False
    ttl_seconds: int = 600


@dataclass
class QaSettings:
    prefer_tricky: bool = True
    include_missing_field: bool = True
    include_unknown_token: bool = True
    include_fairness: bool = True
    include_retries: bool = True


@dataclass
class Policy:
    pack_name: str
    endpoint_method: str
    endpoint_path: str
    status_method: str = "GET"
    status_path: str = "/status"
    max_allowed_concurrent_capacity: int = 2
    duration_seconds: float = 5.0
    cost: CostExpression = dc_field(default_factory=CostExpression)
    isolation: Isolation = dc_field(default_factory=Isolation)
    http: HttpCodes = dc_field(default_factory=HttpCodes)
    idempotency: Idempotency = dc_field(default_factory=Idempotency)
    qa: QaSettings = dc_field(default_factory=QaSettings)
    fail_mode: FailMode = "closed"
    business_flow: str = "generic api endpoint"
    request_example: Dict[str, Any] = dc_field(default_factory=dict)


def load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def policy_to_dict(p: Policy) -> Dict[str, Any]:
    return {
        "pack_name": p.pack_name,
        "business_flow": p.business_flow,
        "request_example": p.request_example,
        "fail_mode": p.fail_mode,
        "http": {
            "protect": {"method": p.endpoint_method.upper(), "path": p.endpoint_path},
            "observe": {"method": p.status_method.upper(), "path": p.status_path},
            "codes": {
                "accepted": p.http.accepted,
                "at_capacity": p.http.at_capacity,
                "bad_request": p.http.bad_request,
            },
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
            "missing_field": {"action": p.cost.missing_field_action, "default_cost": int(p.cost.missing_field_default_cost)},
            "case_insensitive": bool(p.cost.case_insensitive),
        },
        "idempotency": {"enabled": bool(p.idempotency.enabled), "ttl_seconds": int(p.idempotency.ttl_seconds)},
        "qa": {
            "prefer_tricky": bool(p.qa.prefer_tricky),
            "include_missing_field": bool(p.qa.include_missing_field),
            "include_unknown_token": bool(p.qa.include_unknown_token),
            "include_fairness": bool(p.qa.include_fairness),
            "include_retries": bool(p.qa.include_retries),
        },
    }


# Human-readable authoring view used by the wizard

def policy_to_authoring_dict(p: Policy) -> Dict[str, Any]:
    weights = p.cost.token_costs or {}
    inv: Dict[int, List[str]] = {}
    for raw_value, weight in weights.items():
        inv.setdefault(int(weight), []).append(str(raw_value))
    cheap = sorted(inv.get(1, []))
    normal = sorted(inv.get(2, []))
    expensive = sorted(inv.get(4, []))
    very_expensive = sorted(inv.get(8, []))
    return {
        "name": p.pack_name,
        "protect": {
            "flow": p.business_flow,
            "endpoint": {"method": p.endpoint_method.upper(), "path": p.endpoint_path},
        },
        "observe": {
            "endpoint": {"method": p.status_method.upper(), "path": p.status_path},
            "maps_to": "observe",
        },
        "query": {"example": p.request_example},
        "understand": {
            "cost_driver": p.cost.field,
            "weights": {
                "cheap": cheap,
                "normal": normal,
                "expensive": expensive,
                "very_expensive": very_expensive,
            },
            "missing_field": {
                "action": p.cost.missing_field_action,
                "default_cost": p.cost.missing_field_default_cost,
            },
            "unknown_value": {
                "action": p.cost.unknown_action,
                "default_cost": p.cost.unknown_default_cost,
            },
        },
        "fairness": {"per": p.isolation.field if p.isolation.enabled else "shared"},
        "budget": {
            "safely_handle": {"up_to": p.max_allowed_concurrent_capacity, "hold_for": f"{p.duration_seconds:g}s"},
        },
        "when_busy": {"action": "reject immediately", "status": p.http.at_capacity},
        "idempotency": {"enabled": p.idempotency.enabled, "ttl_seconds": p.idempotency.ttl_seconds},
        "qa": {
            "prefer_tricky": p.qa.prefer_tricky,
            "include_missing_field": p.qa.include_missing_field,
            "include_unknown_token": p.qa.include_unknown_token,
            "include_fairness": p.qa.include_fairness,
            "include_retries": p.qa.include_retries,
        },
    }


def policy_from_dict(d: Dict[str, Any]) -> Policy:
    http = d.get("http", {})
    protect = http.get("protect") or http.get("endpoint") or {}
    observe = http.get("observe") or {}
    codes = http.get("codes", {})
    cap = d.get("capacity", {})
    iso = cap.get("isolation", {}) or {}
    cost = d.get("cost_expression", {}) or {}
    unknown = cost.get("unknown", {}) or {}
    missing = cost.get("missing_field", {}) or {}
    idem = d.get("idempotency", {}) or {}
    qa = d.get("qa", {}) or {}
    return Policy(
        pack_name=d["pack_name"],
        business_flow=d.get("business_flow", "generic api endpoint"),
        request_example=d.get("request_example") or {},
        fail_mode=d.get("fail_mode", "closed"),
        endpoint_method=protect.get("method", "POST"),
        endpoint_path=protect.get("path", "/order"),
        status_method=observe.get("method", d.get("status_method", "GET")),
        status_path=observe.get("path", d.get("status_path", "/status")),
        max_allowed_concurrent_capacity=int(cap.get("max_allowed_concurrent_capacity", 2)),
        duration_seconds=float(cap.get("duration_seconds", 5.0)),
        cost=CostExpression(
            field=cost.get("field"),
            separators=cost.get("separators") or ["|", " OR ", " or "],
            combine_mode=cost.get("combine_mode", "choice_max"),
            token_costs=cost.get("token_costs") or {},
            unknown_action=unknown.get("action", "default_cost"),
            unknown_default_cost=int(unknown.get("default_cost", 1)),
            missing_field_action=missing.get("action", "default_cost"),
            missing_field_default_cost=int(missing.get("default_cost", 1)),
            case_insensitive=bool(cost.get("case_insensitive", True)),
        ),
        isolation=Isolation(enabled=bool(iso.get("enabled", False)), field=iso.get("field")),
        http=HttpCodes(
            accepted=int(codes.get("accepted", 200)),
            at_capacity=int(codes.get("at_capacity", 429)),
            bad_request=int(codes.get("bad_request", 400)),
        ),
        idempotency=Idempotency(enabled=bool(idem.get("enabled", False)), ttl_seconds=int(idem.get("ttl_seconds", 600))),
        qa=QaSettings(
            prefer_tricky=bool(qa.get("prefer_tricky", True)),
            include_missing_field=bool(qa.get("include_missing_field", True)),
            include_unknown_token=bool(qa.get("include_unknown_token", True)),
            include_fairness=bool(qa.get("include_fairness", True)),
            include_retries=bool(qa.get("include_retries", True)),
        ),
    )


def load_policy_yaml(policy_yaml: Path) -> Policy:
    return policy_from_dict(load_yaml(policy_yaml))


def compile_policy(policy_yaml: Path, out_json: Path) -> None:
    p = load_policy_yaml(policy_yaml)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(policy_to_dict(p), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
