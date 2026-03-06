from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import requests
import yaml


QADetails = Dict[str, Any]


def save_scenarios(path: Path, scenarios: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(scenarios, sort_keys=False), encoding="utf-8")


def _case_variants(value: str) -> List[str]:
    if not isinstance(value, str) or not value:
        return []
    out: List[str] = []
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


def _normalized_url(base_url: str, path: str) -> str:
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = "http://" + base_url
    if not path.startswith("/"):
        path = "/" + path
    return base_url.rstrip("/") + path


def _timed_http(method: str, url: str, payload: Dict[str, Any], timeout: float = 5.0) -> tuple[requests.Response, float]:
    started = time.perf_counter()
    response = requests.request(method.upper(), url, json=payload, timeout=timeout)
    latency_ms = (time.perf_counter() - started) * 1000.0
    return response, latency_ms


def _latency_summary(latencies_ms: List[float]) -> Dict[str, Any]:
    values = [round(float(x), 2) for x in latencies_ms if x is not None]
    if not values:
        return {
            "count": 0,
            "min_ms": None,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    ordered = sorted(values)

    def percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        idx = (len(ordered) - 1) * p
        lo = int(idx)
        hi = min(lo + 1, len(ordered) - 1)
        frac = idx - lo
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 2)

    return {
        "count": len(values),
        "min_ms": round(min(values), 2),
        "avg_ms": round(sum(values) / len(values), 2),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(max(values), 2),
    }


def generate_scenarios(policy: Any, sample: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        normal_value = sorted_ladder[1][0] if len(sorted_ladder) >= 2 else cheap_value

    def make_payload(value: Any = None) -> Dict[str, Any]:
        p = dict(sample)
        if field and value is not None:
            p[field] = value
        return p

    if field and cheap_value is not None:
        scenarios.append({
            "name": "cheap_request_accepts",
            "why": "Cheapest known value should be accepted when capacity is available.",
            "payload": make_payload(cheap_value),
            "expect": 200,
        })

    if field and heavy_value is not None:
        scenarios.append({
            "name": "heavy_request_accepts",
            "why": "Heavy known value should still be accepted when sent alone if it fits budget.",
            "payload": make_payload(heavy_value),
            "expect": 200,
        })

    if field and cheap_value is not None:
        for v in _case_variants(str(cheap_value)):
            scenarios.append({
                "name": f"case_variant_{v}",
                "why": "Known values must match case-insensitively.",
                "payload": make_payload(v),
                "expect": 200,
            })

    if field:
        unknown_action = _policy_get(
            policy,
            "unknown_action",
            "cost.unknown_action",
            "cost_expression.unknown.action",
            default="reject",
        )
        scenarios.append({
            "name": "unexpected_value_behavior",
            "why": "Totally unexpected values must follow the configured validation behavior.",
            "payload": make_payload("totally_unexpected_value"),
            "expect": bad_request_status if unknown_action in {"reject", "reject_400"} else 200,
        })

        missing_payload = dict(sample)
        if field in missing_payload:
            del missing_payload[field]

        missing_action = _policy_get(
            policy,
            "missing_action",
            "cost.missing_field_action",
            "cost_expression.missing_field.action",
            default="reject",
        )
        scenarios.append({
            "name": "missing_weight_field_behavior",
            "why": "Missing weight-driving field must follow the configured validation behavior.",
            "payload": missing_payload,
            "expect": bad_request_status if missing_action in {"reject", "reject_400"} else 200,
        })

    scenarios.append({
        "name": "overflow_capacity",
        "why": "More than the safe budget must cause overflow rejections.",
        "payload": make_payload(cheap_value),
        "expect": 429,
        "concurrency_test": True,
    })

    if field and cheap_value is not None and heavy_value is not None and cheap_value != heavy_value:
        scenarios.append({
            "name": "mixed_workload_capacity",
            "why": "Mixed cheap and heavy requests must still respect the same hard budget.",
            "mix_test": True,
            "mix_values": [cheap_value, heavy_value, normal_value or cheap_value],
            "expect_any": [200, 429],
        })

    scenarios.append({
        "name": "randomized_load_simulation",
        "why": "Random load with mixed known values, case variants, and unexpected values should never break admission rules.",
        "simulation_test": True,
        "simulation_rounds": max(12, max_capacity * 4),
        "expect_any": [200, 429, bad_request_status],
    })

    return scenarios


def run_scenarios(
    base_url: str,
    policy: Any,
    scenarios: List[Dict[str, Any]],
    progress: Callable[[str], None] | None = None,
) -> tuple[bool, List[Dict[str, Any]], QADetails]:
    errors: List[Dict[str, Any]] = []
    scenario_results: List[Dict[str, Any]] = []
    all_latencies_ms: List[float] = []
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
    ladders = _policy_get(policy, "weight_ladder", "cost.token_costs", "cost_expression.token_costs", default={}) or {}
    max_capacity = int(_policy_get(policy, "max_allowed_concurrent_capacity", "capacity.max", "capacity.max_allowed_concurrent_capacity", default=5))
    fairness_enabled = bool(_policy_get(policy, "isolation.enabled", "capacity.isolation.enabled", default=False))
    fairness_field = _policy_get(policy, "isolation.field", "capacity.isolation.field")
    hold_seconds = float(_policy_get(policy, "duration_seconds", "capacity.duration_seconds", default=0.0) or 0.0)

    url = _normalized_url(base_url, path)
    known_values = list(ladders.keys())

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    def emit_latency_summary(prefix: str, latencies: List[float]) -> None:
        summary = _latency_summary(latencies)
        if summary["count"] == 0:
            return
        emit(
            f"{prefix}count={summary['count']} min={summary['min_ms']}ms avg={summary['avg_ms']}ms "
            f"p50={summary['p50_ms']}ms p95={summary['p95_ms']}ms max={summary['max_ms']}ms"
        )

    def with_actor(payload: Dict[str, Any], actor_value: str) -> Dict[str, Any]:
        p = dict(payload)
        if fairness_enabled and fairness_field:
            p[fairness_field] = actor_value
        return p

    def maybe_wait_for_release() -> None:
        if not fairness_enabled and hold_seconds > 0:
            time.sleep(hold_seconds + 0.05)

    emit(f"Target URL: {url}")
    emit(f"HTTP method: {method}")
    emit("")

    for i, sc in enumerate(scenarios, start=1):
        emit(f"[{i}/{len(scenarios)}] Running scenario: {sc['name']}")
        emit(f"Why: {sc.get('why', 'No explanation provided.')}")

        try:
            if sc.get("concurrency_test"):
                maybe_wait_for_release()
                results: List[Any] = []
                latencies: List[float] = []
                lock = threading.Lock()
                threads: List[threading.Thread] = []
                shared_actor = f"capax_{sc['name']}_shared"

                def worker(payload: Dict[str, Any]) -> None:
                    try:
                        response, latency_ms = _timed_http(method, url, payload, timeout=5)
                        with lock:
                            results.append(response.status_code)
                            latencies.append(latency_ms)
                    except Exception as exc:
                        with lock:
                            results.append(f"ERROR:{exc}")

                worker_count = max_capacity + 3
                for n in range(worker_count):
                    payload = with_actor(dict(sc.get("payload", sample_request)), shared_actor)
                    payload["_capaxQaNonce"] = f"overflow_{n}"
                    t = threading.Thread(target=worker, args=(payload,))
                    threads.append(t)
                    t.start()

                for t in threads:
                    t.join()

                all_latencies_ms.extend(latencies)
                emit(f"  concurrency results: {results}")
                emit_latency_summary("  concurrency latency: ", latencies)
                passed = (sc["expect"] in results) and (200 in results)
                if not passed:
                    ok = False
                    errors.append({
                        "scenario": sc["name"],
                        "error": "expected_concurrency_mix_missing",
                        "expected": sc["expect"],
                        "results": results,
                        "latency_summary": _latency_summary(latencies),
                    })
                    emit(f"  FAIL - expected at least one 200 and one {sc['expect']}")
                else:
                    emit("  PASS")
                scenario_results.append({
                    "name": sc["name"],
                    "kind": "concurrency_test",
                    "results": results,
                    "latency_summary": _latency_summary(latencies),
                    "passed": passed,
                })

            elif sc.get("mix_test"):
                maybe_wait_for_release()
                results: List[int] = []
                latencies: List[float] = []
                actor = f"capax_{sc['name']}_shared"
                for idx, value in enumerate(sc.get("mix_values", [])):
                    payload = dict(sample_request)
                    if field:
                        payload[field] = value
                    payload = with_actor(payload, actor)
                    payload["_capaxQaNonce"] = f"mix_{idx}"
                    response, latency_ms = _timed_http(method, url, payload, timeout=5)
                    results.append(response.status_code)
                    latencies.append(latency_ms)
                    all_latencies_ms.append(latency_ms)
                    emit(f"  mix value={value!r} -> status {response.status_code}")
                    emit(f"  latency: {latency_ms:.2f}ms")
                passed = 200 in results and 429 in results
                if not passed:
                    ok = False
                    errors.append({
                        "scenario": sc["name"],
                        "error": "mixed_results_unexpected",
                        "results": results,
                        "allowed": sc["expect_any"],
                        "latency_summary": _latency_summary(latencies),
                    })
                    emit("  FAIL - expected to observe both admitted and rejected mixed requests")
                else:
                    emit("  PASS")
                emit_latency_summary("  mixed latency: ", latencies)
                scenario_results.append({
                    "name": sc["name"],
                    "kind": "mix_test",
                    "results": results,
                    "latency_summary": _latency_summary(latencies),
                    "passed": passed,
                })

            elif sc.get("simulation_test"):
                maybe_wait_for_release()
                rounds = int(sc.get("simulation_rounds", 20))
                seen: List[int] = []
                latencies: List[float] = []
                for n in range(rounds):
                    payload = dict(sample_request)
                    if field:
                        choice_pool: List[Any] = []
                        for kv in known_values:
                            choice_pool.append(kv)
                            choice_pool.extend(_case_variants(str(kv)))
                        choice_pool.append("totally_unexpected_value")
                        value = random.choice(choice_pool) if choice_pool else "totally_unexpected_value"
                        payload[field] = value
                    payload = with_actor(payload, f"capax_sim_{n}")
                    payload["_capaxQaNonce"] = f"sim_{n}"
                    response, latency_ms = _timed_http(method, url, payload, timeout=5)
                    seen.append(response.status_code)
                    latencies.append(latency_ms)
                    all_latencies_ms.append(latency_ms)
                    emit(f"  simulation {n+1}/{rounds}: status {response.status_code}")
                    emit(f"  latency: {latency_ms:.2f}ms")
                allowed = set(sc["expect_any"])
                passed = not any(status not in allowed for status in seen)
                if not passed:
                    ok = False
                    errors.append({
                        "scenario": sc["name"],
                        "error": "simulation_unexpected_status",
                        "results": seen,
                        "allowed": list(allowed),
                        "latency_summary": _latency_summary(latencies),
                    })
                    emit(f"  FAIL - unexpected status in simulation results {seen}")
                else:
                    emit("  PASS")
                emit_latency_summary("  simulation latency: ", latencies)
                scenario_results.append({
                    "name": sc["name"],
                    "kind": "simulation_test",
                    "results": seen,
                    "latency_summary": _latency_summary(latencies),
                    "passed": passed,
                })

            else:
                maybe_wait_for_release()
                payload = with_actor(dict(sc.get("payload", sample_request)), f"capax_{sc['name']}")
                payload["_capaxQaNonce"] = sc["name"]
                emit(f"  payload: {json.dumps(payload, ensure_ascii=False)}")
                response, latency_ms = _timed_http(method, url, payload, timeout=5)
                all_latencies_ms.append(latency_ms)
                emit(f"  status: {response.status_code}")
                emit(f"  latency: {latency_ms:.2f}ms")
                passed = response.status_code == sc["expect"]
                if not passed:
                    ok = False
                    errors.append({
                        "scenario": sc["name"],
                        "expected": sc["expect"],
                        "got": response.status_code,
                        "body": response.text,
                        "latency_ms": round(latency_ms, 2),
                    })
                    emit(f"  FAIL - expected {sc['expect']}, got {response.status_code}")
                else:
                    emit("  PASS")
                scenario_results.append({
                    "name": sc["name"],
                    "kind": "single_request",
                    "status": response.status_code,
                    "expected": sc["expect"],
                    "latency_ms": round(latency_ms, 2),
                    "passed": passed,
                })

        except Exception as exc:
            ok = False
            errors.append({"scenario": sc["name"], "error": str(exc)})
            scenario_results.append({"name": sc["name"], "error": str(exc), "passed": False})
            emit(f"  ERROR: {exc}")

        emit("")

    overall_latency = _latency_summary(all_latencies_ms)
    if overall_latency["count"]:
        emit(
            f"Overall latency: count={overall_latency['count']} min={overall_latency['min_ms']}ms "
            f"avg={overall_latency['avg_ms']}ms p50={overall_latency['p50_ms']}ms "
            f"p95={overall_latency['p95_ms']}ms max={overall_latency['max_ms']}ms"
        )

    details: QADetails = {
        "target": url,
        "http_method": method,
        "scenario_results": scenario_results,
        "latency_summary": overall_latency,
        "total_http_calls": int(overall_latency["count"]),
    }
    return ok, errors, details


def save_report(
    path: Path,
    ok: bool,
    errors: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
    url: str,
    details: Dict[str, Any] | None = None,
) -> None:
    details = details or {}
    report = {
        "target": details.get("target", url),
        "http_method": details.get("http_method"),
        "ok": ok,
        "errors": errors,
        "scenarios": scenarios,
        "scenario_results": details.get("scenario_results", []),
        "latency_summary": details.get("latency_summary"),
        "total_http_calls": details.get("total_http_calls"),
        "timestamp": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

