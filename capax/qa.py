from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import requests
import yaml


def save_scenarios(path: Path, scenarios: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(scenarios, sort_keys=False), encoding="utf-8")


def _case_variants(value: str) -> List[str]:
    if not isinstance(value, str) or not value:
        return []
    out = []
    candidates = [
        value.upper(),
        value.lower(),
        value.capitalize(),
        "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(value)),
    ]
    for c in candidates:
        if c != value and c not in out:
            out.append(c)
    return out


def _policy_get(policy: Any, *paths: str, default: Any = None) -> Any:
    """
    Try multiple dotted paths and return the first match.
    Supports both dict-shaped compiled policies and Policy/dataclass objects used in the wizard.
    """
    for path in paths:
        cur: Any = policy
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif hasattr(cur, part):
                cur = getattr(cur, part)
            else:
                ok = False
                break
        if ok:
            return cur
    return default


def _ensure_policy_dict(policy: Any) -> Any:
    return policy


def generate_scenarios(policy: Any, sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    policy = _ensure_policy_dict(policy)
    scenarios: List[Dict[str, Any]] = []

    ladders = _policy_get(
        policy,
        "weight_ladder",
        "cost.token_costs",
        "cost_expression.token_costs",
        default={},
    ) or {}
    field = _policy_get(policy, "weight_field", "cost.field", "cost_expression.field")
    max_capacity = int(
        _policy_get(
            policy,
            "max_allowed_concurrent_capacity",
            "capacity.max",
            "capacity.max_allowed_concurrent_capacity",
            default=5,
        )
    )
    bad_request_status = int(
        _policy_get(
            policy,
            "bad_request_status",
            "http.bad_request",
            "http.codes.bad_request",
            default=400,
        )
    )

    cheap_value = None
    heavy_value = None
    normal_value = None

    if ladders:
        sorted_ladder = sorted(ladders.items(), key=lambda x: x[1])
        cheap_value = sorted_ladder[0][0]
        heavy_value = sorted_ladder[-1][0]
        if len(sorted_ladder) >= 3:
            normal_value = sorted_ladder[1][0]
        elif len(sorted_ladder) >= 2:
            normal_value = sorted_ladder[1][0]
        else:
            normal_value = cheap_value

    def make_payload(value: Any = None) -> Dict[str, Any]:
        p = sample.copy()
        if field and value is not None:
            p[field] = value
        return p

    if field and cheap_value is not None:
        scenarios.append(
            {
                "name": "cheap_request_accepts",
                "why": "Cheapest known value should be accepted when capacity is available.",
                "payload": make_payload(cheap_value),
                "expect": 200,
            }
        )

    if field and heavy_value is not None:
        scenarios.append(
            {
                "name": "heavy_request_accepts",
                "why": "Heavy known value should still be accepted when sent alone if it fits budget.",
                "payload": make_payload(heavy_value),
                "expect": 200,
            }
        )

    if field and cheap_value is not None:
        for v in _case_variants(str(cheap_value)):
            scenarios.append(
                {
                    "name": f"case_variant_{v}",
                    "why": "Known values must match case-insensitively.",
                    "payload": make_payload(v),
                    "expect": 200,
                }
            )

    if field:
        unknown_action = _policy_get(
            policy,
            "unknown_action",
            "cost.unknown_action",
            "cost_expression.unknown.action",
            default="reject",
        )
        scenarios.append(
            {
                "name": "unexpected_value_behavior",
                "why": "Totally unexpected values must follow the configured validation behavior.",
                "payload": make_payload("totally_unexpected_value"),
                "expect": bad_request_status if unknown_action in {"reject", "reject_400"} else 200,
            }
        )

        missing_payload = sample.copy()
        if field in missing_payload:
            del missing_payload[field]

        missing_action = _policy_get(
            policy,
            "missing_action",
            "cost.missing_field_action",
            "cost_expression.missing_field.action",
            default="reject",
        )
        scenarios.append(
            {
                "name": "missing_weight_field_behavior",
                "why": "Missing weight-driving field must follow the configured validation behavior.",
                "payload": missing_payload,
                "expect": bad_request_status if missing_action in {"reject", "reject_400"} else 200,
            }
        )

    scenarios.append(
        {
            "name": "overflow_capacity",
            "why": "More than the safe budget must cause overflow rejections.",
            "payload": make_payload(cheap_value),
            "expect": 429,
            "concurrency_test": True,
        }
    )

    if field and cheap_value is not None and heavy_value is not None and cheap_value != heavy_value:
        scenarios.append(
            {
                "name": "mixed_workload_capacity",
                "why": "Mixed cheap and heavy requests must still respect the same hard budget.",
                "mix_test": True,
                "mix_values": [cheap_value, heavy_value, normal_value or cheap_value],
                "expect_any": [200, 429],
            }
        )

    scenarios.append(
        {
            "name": "randomized_load_simulation",
            "why": "Random load with mixed known values, case variants, and unexpected values should never break admission rules.",
            "simulation_test": True,
            "simulation_rounds": max(12, max_capacity * 4),
            "expect_any": [200, 429, bad_request_status],
        }
    )

    return scenarios


def _normalized_url(base_url: str, path: str) -> str:
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = "http://" + base_url
    if not path.startswith("/"):
        path = "/" + path
    return base_url.rstrip("/") + path


def _http(method: str, url: str, payload: Dict[str, Any], timeout: float = 5.0) -> requests.Response:
    return requests.request(method.upper(), url, json=payload, timeout=timeout)


def _scenario_actor(scenario_name: str, suffix: str = "") -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in scenario_name).strip("_") or "scenario"
    return f"capax_{token}{suffix}"


def _apply_isolation_actor(payload: Dict[str, Any], isolation_field: str | None, actor_value: str | None) -> Dict[str, Any]:
    if isolation_field and actor_value is not None:
        payload[isolation_field] = actor_value
    return payload


def _sleep_for_release(seconds: float, emit: Callable[[str], None], reason: str) -> None:
    delay = max(0.0, float(seconds))
    if delay <= 0:
        return
    emit(f"  waiting {delay:.2f}s so previous accepted work releases capacity before {reason}")
    time.sleep(delay)


def run_scenarios(
    base_url: str,
    policy: Any,
    scenarios: List[Dict[str, Any]],
    progress: Callable[[str], None] | None = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    policy = _ensure_policy_dict(policy)
    errors: List[Dict[str, Any]] = []
    ok = True

    method = str(
        _policy_get(
            policy,
            "method",
            "endpoint_method",
            "http.protect.method",
            "protect.endpoint.method",
            "protect.entry.method",
            default="POST",
        )
    ).upper()

    path = str(
        _policy_get(
            policy,
            "path",
            "endpoint_path",
            "http.protect.path",
            "protect.endpoint.path",
            "protect.entry.path",
            default="/",
        )
    )

    sample_request = _policy_get(policy, "sample_request", "request_example", "query.example", default={}) or {}
    field = _policy_get(policy, "weight_field", "cost.field", "cost_expression.field")
    ladders = _policy_get(
        policy,
        "weight_ladder",
        "cost.token_costs",
        "cost_expression.token_costs",
        default={},
    ) or {}
    accepted_status = int(
        _policy_get(
            policy,
            "accepted_status",
            "http.accepted",
            "http.codes.accepted",
            default=200,
        )
    )
    bad_request_status = int(
        _policy_get(
            policy,
            "bad_request_status",
            "http.bad_request",
            "http.codes.bad_request",
            default=400,
        )
    )
    max_capacity = int(
        _policy_get(
            policy,
            "max_allowed_concurrent_capacity",
            "capacity.max",
            "capacity.max_allowed_concurrent_capacity",
            default=5,
        )
    )
    hold_seconds = float(
        _policy_get(
            policy,
            "duration_seconds",
            "capacity.duration_seconds",
            default=0.0,
        )
    )
    isolation_enabled = bool(
        _policy_get(
            policy,
            "capacity.isolation.enabled",
            "isolation.enabled",
            default=False,
        )
    )
    isolation_field = _policy_get(
        policy,
        "capacity.isolation.field",
        "isolation.field",
        default=None,
    )

    url = _normalized_url(base_url, path)

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    emit(f"Target URL: {url}")
    emit(f"HTTP method: {method}")
    emit("")

    known_values = list(ladders.keys())
    must_drain_between_scenarios = not (isolation_enabled and isolation_field)

    for i, sc in enumerate(scenarios, start=1):
        emit(f"[{i}/{len(scenarios)}] Running scenario: {sc['name']}")
        emit(f"Why: {sc.get('why', 'No explanation provided.')}" )

        if i > 1 and must_drain_between_scenarios:
            _sleep_for_release(hold_seconds + 0.05, emit, sc["name"])

        try:
            if sc.get("concurrency_test"):
                results: List[Any] = []
                threads: List[threading.Thread] = []
                actor_value = _scenario_actor(sc["name"])

                def worker(payload: Dict[str, Any]) -> None:
                    try:
                        r = _http(method, url, payload, timeout=5)
                        results.append(r.status_code)
                    except Exception as e:
                        results.append(f"ERROR:{e}")

                worker_count = max_capacity + 3
                for n in range(worker_count):
                    payload = dict(sc.get("payload") or sample_request)
                    payload = _apply_isolation_actor(payload, isolation_field if isolation_enabled else None, actor_value)
                    payload["_capaxQaNonce"] = f"overflow_{n}"
                    t = threading.Thread(target=worker, args=(payload,))
                    threads.append(t)
                    t.start()

                for t in threads:
                    t.join()

                emit(f"  concurrency results: {results}")

                missing = []
                if sc["expect"] not in results:
                    missing.append(sc["expect"])
                if accepted_status not in results:
                    missing.append(accepted_status)

                if missing:
                    ok = False
                    errors.append(
                        {
                            "scenario": sc["name"],
                            "error": "unexpected_concurrency_profile",
                            "expected_to_see": [accepted_status, sc["expect"]],
                            "results": results,
                        }
                    )
                    emit(f"  FAIL - expected to see both {accepted_status} and {sc['expect']} in results")
                else:
                    emit("  PASS")

            elif sc.get("mix_test"):
                results: List[int] = []
                values = sc.get("mix_values", [])
                actor_value = _scenario_actor(sc["name"])
                for idx, value in enumerate(values):
                    payload = dict(sample_request)
                    if field:
                        payload[field] = value
                    payload = _apply_isolation_actor(payload, isolation_field if isolation_enabled else None, actor_value)
                    payload["_capaxQaNonce"] = f"mix_{idx}"
                    r = _http(method, url, payload, timeout=5)
                    results.append(r.status_code)
                    emit(f"  mix value={value!r} -> status {r.status_code}")

                allowed = set(sc["expect_any"])
                unexpected = [status for status in results if status not in allowed]
                has_accept = accepted_status in results
                has_overflow = 429 in results
                if unexpected or not has_accept or not has_overflow:
                    ok = False
                    errors.append(
                        {
                            "scenario": sc["name"],
                            "error": "mixed_results_unexpected",
                            "results": results,
                            "allowed": list(allowed),
                            "expected_profile": [accepted_status, 429],
                        }
                    )
                    emit(
                        f"  FAIL - expected mixed workload to show at least one {accepted_status} and one 429 without other statuses"
                    )
                else:
                    emit("  PASS")

            elif sc.get("simulation_test"):
                rounds = int(sc.get("simulation_rounds", 20))
                seen: List[int] = []
                rnd = random.Random(0)
                for n in range(rounds):
                    payload = dict(sample_request)
                    if field:
                        choice_pool: List[Any] = []
                        for kv in known_values:
                            choice_pool.append(kv)
                            choice_pool.extend(_case_variants(str(kv)))
                        choice_pool.append("totally_unexpected_value")
                        value = rnd.choice(choice_pool) if choice_pool else "totally_unexpected_value"
                        payload[field] = value
                    actor_value = _scenario_actor(sc["name"], f"_{n}") if isolation_enabled and isolation_field else None
                    payload = _apply_isolation_actor(payload, isolation_field if isolation_enabled else None, actor_value)
                    payload["_capaxQaNonce"] = f"sim_{n}"

                    r = _http(method, url, payload, timeout=5)
                    seen.append(r.status_code)
                    emit(f"  simulation {n+1}/{rounds}: status {r.status_code}")

                allowed = set(sc["expect_any"])
                if any(status not in allowed for status in seen):
                    ok = False
                    errors.append(
                        {
                            "scenario": sc["name"],
                            "error": "simulation_unexpected_status",
                            "results": seen,
                            "allowed": list(allowed),
                        }
                    )
                    emit(f"  FAIL - unexpected status in simulation results {seen}")
                elif known_values and isolation_enabled and isolation_field and accepted_status not in seen:
                    ok = False
                    errors.append(
                        {
                            "scenario": sc["name"],
                            "error": "simulation_missing_accept",
                            "results": seen,
                            "expected_to_see": accepted_status,
                        }
                    )
                    emit(f"  FAIL - expected at least one valid request to be accepted with status {accepted_status}")
                else:
                    emit("  PASS")

            else:
                payload = dict(sc.get("payload") or {})
                if not payload:
                    payload = dict(sample_request)
                actor_value = _scenario_actor(sc["name"]) if isolation_enabled and isolation_field else None
                payload = _apply_isolation_actor(payload, isolation_field if isolation_enabled else None, actor_value)
                payload["_capaxQaNonce"] = sc["name"]

                emit(f"  payload: {json.dumps(payload, ensure_ascii=False)}")
                r = _http(method, url, payload, timeout=5)
                emit(f"  status: {r.status_code}")

                if r.status_code != sc["expect"]:
                    ok = False
                    errors.append(
                        {
                            "scenario": sc["name"],
                            "expected": sc["expect"],
                            "got": r.status_code,
                            "body": r.text,
                        }
                    )
                    emit(f"  FAIL - expected {sc['expect']}, got {r.status_code}")
                else:
                    emit("  PASS")

        except Exception as e:
            ok = False
            errors.append({"scenario": sc["name"], "error": str(e)})
            emit(f"  ERROR: {e}")

        emit("")

    return ok, errors


def save_report(path: Path, ok: bool, errors: List[Dict[str, Any]], scenarios: List[Dict[str, Any]], url: str) -> None:
    report = {
        "target": url,
        "ok": ok,
        "errors": errors,
        "scenarios": scenarios,
        "timestamp": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

