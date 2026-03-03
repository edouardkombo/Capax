from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

import requests

from .config import Policy


@dataclass
class Scenario:
    name: str
    steps: List[Dict[str, Any]]


def _make_body(sample: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    b = dict(sample)
    b.update(updates)
    return b


def _nonce(i: int) -> str:
    return f"qa_{int(time.time()*1000)}_{i}"


def compute_cost_from_policy(policy: Policy, body: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    field = policy.cost.field
    if not field:
        return 1, None
    raw = body.get(field)
    if raw is None:
        return 1, None

    token_costs = policy.cost.token_costs or {}
    token_costs_ci = {str(k).casefold(): int(v) for k, v in token_costs.items()}
    unknown = policy.cost.unknown_action
    unknown_default = policy.cost.unknown_default_cost

    def token_cost(tok: str) -> Tuple[Optional[int], Optional[str]]:
        if tok in token_costs:
            return int(token_costs[tok]), None
        tcf = tok.casefold()
        if tcf in token_costs_ci:
            return int(token_costs_ci[tcf]), None
        if unknown == "reject_400":
            return None, f"unknown_token:{tok}"
        return int(unknown_default), None

    if not isinstance(raw, str):
        return token_cost(str(raw))

    s = raw.strip()
    if not s:
        return 1, None

    tokens = [s]
    for sep in policy.cost.separators:
        nxt: List[str] = []
        for t in tokens:
            if sep in t:
                nxt.extend([x.strip() for x in t.split(sep) if x.strip()])
            else:
                nxt.append(t.strip())
        tokens = nxt

    seen = set()
    uniq: List[str] = []
    for t in tokens:
        if t and t not in seen:
            uniq.append(t)
            seen.add(t)

    costs: List[int] = []
    for tok in uniq:
        c, err = token_cost(tok)
        if err:
            return None, err
        costs.append(int(c))  # type: ignore

    if not costs:
        return 1, None
    if len(costs) == 1:
        return costs[0], None

    if policy.cost.combine_mode == "bundle_sum":
        return sum(costs), None
    return max(costs), None


def generate_scenarios(policy: Policy, sample_body: Dict[str, Any]) -> List[Scenario]:
    scenarios: List[Scenario] = []
    token_costs = policy.cost.token_costs or {}
    if not policy.cost.field or not token_costs:
        token_costs = {"_default": 1}

    cheapest_tok = min(token_costs.keys(), key=lambda k: token_costs[k])
    heaviest_tok = max(token_costs.keys(), key=lambda k: token_costs[k])

    cheapest_cost = int(token_costs.get(cheapest_tok, 1))
    heaviest_cost = int(token_costs.get(heaviest_tok, 1))
    cap = int(policy.max_allowed_concurrent_capacity)

    def body_for_token(tok: str, i: int) -> Dict[str, Any]:
        u = {"_qaNonce": _nonce(i)}
        if policy.cost.field and tok != "_default":
            u[policy.cost.field] = tok
        return _make_body(sample_body, u)

    # Fill with cheapest until full, then one more should be 429.
    max_cheapest = max(1, cap // max(1, cheapest_cost))
    steps = []
    for i in range(max_cheapest):
        steps.append({"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(cheapest_tok, i), "expect_status": policy.http.accepted})
    steps.append({"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(cheapest_tok, max_cheapest), "expect_status": policy.http.at_capacity})
    scenarios.append(Scenario("fill_with_cheapest_then_overflow", steps))

    # Mixed boundary: start with heaviest, then fill remainder with cheapest, then overflow.
    if heaviest_cost <= cap:
        k = max(0, (cap - heaviest_cost) // max(1, cheapest_cost))
        steps = [{"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(heaviest_tok, 100), "expect_status": policy.http.accepted}]
        for j in range(k):
            steps.append({"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(cheapest_tok, 101 + j), "expect_status": policy.http.accepted})
        steps.append({"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(cheapest_tok, 200), "expect_status": policy.http.at_capacity})
        scenarios.append(Scenario("mixed_boundary_then_overflow", steps))
    else:
        scenarios.append(Scenario("heaviest_always_rejected", [
            {"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(heaviest_tok, 300), "expect_status": policy.http.at_capacity}
        ]))

    scenarios.append(Scenario("telemetry_reflects_completion", [
        {"method": policy.endpoint_method, "path": policy.endpoint_path, "body": body_for_token(cheapest_tok, 400), "expect_status": policy.http.accepted},
        {"method": "GET", "path": policy.status_path, "body": None, "expect_status": 200, "expect_contains": {"servedOrders_len_at_least": 0}},
        {"wait_s": float(policy.duration_seconds) + 0.25},
        {"method": "GET", "path": policy.status_path, "body": None, "expect_status": 200, "expect_contains": {"servedOrders_len_at_least": 1}},
    ]))

    return scenarios


def _describe_cost(policy: Policy, body: Dict[str, Any]) -> str:
    cost, err = compute_cost_from_policy(policy, body)
    field = policy.cost.field
    if err:
        return f"computed_cost=ERR({err})"
    if not field:
        return f"computed_cost={cost}"
    return f"{field}={body.get(field)!r} computed_cost={cost}"


def run_scenarios(base_url: str, policy: Policy, scenarios: List[Scenario], timeout_s: float = 5.0) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    base = base_url.rstrip("/")
    cap = int(policy.max_allowed_concurrent_capacity)

    drain_wait = float(policy.duration_seconds) + 0.75  # more generous barrier

    for sc in scenarios:
        print(f"Scenario: {sc.name}")
        for idx, step in enumerate(sc.steps):
            if "wait_s" in step:
                w = float(step["wait_s"])
                print(f"  Step {idx}: WAIT {w:.2f}s")
                time.sleep(w)
                continue

            method = step["method"].upper()
            url = f"{base}{step['path']}"
            body = step.get("body", None)
            exp = int(step["expect_status"])

            if method == "GET":
                print(f"  Step {idx}: GET {step['path']} expect={exp}", end="")
            else:
                cost_desc = _describe_cost(policy, body)
                print(f"  Step {idx}: {method} {step['path']} cap={cap} {cost_desc} expect={exp}", end="")

            try:
                if method == "GET":
                    r = requests.get(url, timeout=timeout_s)
                else:
                    r = requests.request(method, url, json=body, timeout=timeout_s)
            except Exception as e:
                print(" -> request failed")
                errors.append(f"{sc.name} step {idx}: request failed: {e}")
                continue

            got = r.status_code
            server_cost = ""
            try:
                js = r.json()
                if isinstance(js, dict) and "cost" in js:
                    server_cost = f" server_cost={js.get('cost')}"
            except Exception:
                pass

            print(f" got={got}{server_cost}")

            if got != exp:
                errors.append(f"{sc.name} step {idx}: expected {exp}, got {got}, body={r.text[:300]}")
                continue

            if method == "GET" and "expect_contains" in step:
                try:
                    js2 = r.json()
                except Exception:
                    errors.append(f"{sc.name} step {idx}: expected JSON, got {r.text[:200]}")
                    continue
                ec = step["expect_contains"]
                if "servedOrders_len_at_least" in ec:
                    if len(js2.get("servedOrders", [])) < int(ec["servedOrders_len_at_least"]):
                        errors.append(f"{sc.name} step {idx}: servedOrders_len_at_least expected {ec['servedOrders_len_at_least']}, got {len(js2.get('servedOrders', []))}")

        print(f"  Drain: WAIT {drain_wait:.2f}s (scenario isolation)")
        time.sleep(drain_wait)

    return (len(errors) == 0), errors
