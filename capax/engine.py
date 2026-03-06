from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import Policy
from .utils import fingerprint_request, now_ms


@dataclass
class OrderRecord:
    order_id: str
    customer_key: str
    body: Dict[str, Any]
    cost: int
    admitted_ms: int
    served_ms: Optional[int] = None
    state: str = "in_flight"


class CapacityState:
    def __init__(self) -> None:
        self.in_flight_cost: int = 0
        self.in_flight: Dict[str, OrderRecord] = {}
        self.served: List[OrderRecord] = []
        self.unique_customers: set[str] = set()
        self.idempotency: Dict[str, Tuple[int, Dict[str, Any]]] = {}


class CapacityGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._lock = asyncio.Lock()
        self._states: Dict[str, CapacityState] = {"shared": CapacityState()}
        self._token_costs_ci: Dict[str, int] = {str(k).casefold(): int(v) for k, v in (policy.cost.token_costs or {}).items()}

    def _get_isolation_key(self, body: Dict[str, Any]) -> str:
        iso = self.policy.isolation
        if not iso.enabled or not iso.field:
            return "shared"
        return f"{iso.field}={body.get(iso.field)}"

    def _split_tokens(self, raw: str) -> List[str]:
        tokens = [raw]
        for sep in self.policy.cost.separators:
            new_tokens: List[str] = []
            for t in tokens:
                if sep in t:
                    new_tokens.extend([x.strip() for x in t.split(sep) if x.strip()])
                else:
                    new_tokens.append(t.strip())
            tokens = new_tokens
        seen = set()
        out: List[str] = []
        for t in tokens:
            if t and t not in seen:
                out.append(t)
                seen.add(t)
        return out

    def _token_cost(self, tok: str) -> Tuple[Optional[int], Optional[str]]:
        m = self.policy.cost.token_costs or {}
        if tok in m:
            return int(m[tok]), None
        tcf = tok.casefold()
        if self.policy.cost.case_insensitive and tcf in self._token_costs_ci:
            return int(self._token_costs_ci[tcf]), None
        if self.policy.cost.unknown_action == "reject_400":
            return None, f"unknown_token:{tok}"
        return int(self.policy.cost.unknown_default_cost), None

    def compute_cost(self, body: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
        field = self.policy.cost.field
        if not field:
            return 1, None
        if field not in body:
            if self.policy.cost.missing_field_action == "reject_400":
                return None, f"missing_field:{field}"
            return int(self.policy.cost.missing_field_default_cost), None
        raw = body.get(field)
        if raw is None:
            if self.policy.cost.missing_field_action == "reject_400":
                return None, f"missing_field:{field}"
            return int(self.policy.cost.missing_field_default_cost), None

        if not isinstance(raw, str):
            return self._token_cost(str(raw))
        raw_s = raw.strip()
        if raw_s == "":
            if self.policy.cost.missing_field_action == "reject_400":
                return None, f"missing_field:{field}"
            return int(self.policy.cost.missing_field_default_cost), None

        tokens = self._split_tokens(raw_s)
        costs: List[int] = []
        for tok in tokens:
            c, err = self._token_cost(tok)
            if err:
                return None, err
            costs.append(int(c))

        if not costs:
            return int(self.policy.cost.missing_field_default_cost), None
        if len(costs) == 1:
            return costs[0], None
        if self.policy.cost.combine_mode == "bundle_sum":
            return sum(costs), None
        return max(costs), None

    def explain_cost(self, body: Dict[str, Any]) -> Dict[str, Any]:
        field = self.policy.cost.field
        cost, err = self.compute_cost(body)
        reason = []
        if not field:
            reason.append("no_cost_field:default=1")
            return {"cost": cost, "reason": reason, "error": err}
        if field not in body or body.get(field) in (None, ""):
            reason.append(f"{field}:missing")
            return {"cost": cost, "reason": reason, "error": err}
        raw = body.get(field)
        if isinstance(raw, str):
            toks = self._split_tokens(raw.strip())
        else:
            toks = [str(raw)]
        reason.extend([f"{field}:{tok}" for tok in toks])
        return {"cost": cost, "reason": reason, "error": err}

    def _state_for(self, key: str) -> CapacityState:
        if key not in self._states:
            self._states[key] = CapacityState()
        return self._states[key]

    def _idempo_get(self, st: CapacityState, fp: str) -> Optional[Dict[str, Any]]:
        if not self.policy.idempotency.enabled:
            return None
        entry = st.idempotency.get(fp)
        if not entry:
            return None
        expires_ms, payload = entry
        if now_ms() > expires_ms:
            st.idempotency.pop(fp, None)
            return None
        return payload

    def _idempo_put(self, st: CapacityState, fp: str, payload: Dict[str, Any]) -> None:
        if not self.policy.idempotency.enabled:
            return
        expires_ms = now_ms() + int(self.policy.idempotency.ttl_seconds) * 1000
        st.idempotency[fp] = (expires_ms, payload)

    async def admit(self, method: str, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        key = self._get_isolation_key(body)
        st = self._state_for(key)
        fp = fingerprint_request(method, path, body)

        async with self._lock:
            cached = self._idempo_get(st, fp)
            if cached is not None:
                status = int(cached.get("_status", self.policy.http.accepted))
                payload = {k: v for k, v in cached.items() if k != "_status"}
                payload["idempotent_replay"] = True
                return status, payload

            cost, err = self.compute_cost(body)
            if err:
                payload = {"result": "rejected", "reason": err, "cost": None, "bucket": key}
                self._idempo_put(st, fp, {"_status": self.policy.http.bad_request, **payload})
                return self.policy.http.bad_request, payload

            cost_i = int(cost or 1)
            cap = int(self.policy.max_allowed_concurrent_capacity)
            if st.in_flight_cost + cost_i > cap:
                payload = {
                    "result": "rejected",
                    "reason": "at_capacity",
                    "cost": cost_i,
                    "bucket": key,
                    "in_flight_cost": st.in_flight_cost,
                    "capacity": cap,
                }
                self._idempo_put(st, fp, {"_status": self.policy.http.at_capacity, **payload})
                return self.policy.http.at_capacity, payload

            order_id = fp[:12]
            rec = OrderRecord(order_id=order_id, customer_key=key, body=body, cost=cost_i, admitted_ms=now_ms())
            st.in_flight_cost += cost_i
            st.in_flight[order_id] = rec
            st.unique_customers.add(key)

            asyncio.create_task(self._release_later(key, order_id, float(self.policy.duration_seconds)))

            payload = {
                "result": "accepted",
                "orderId": order_id,
                "cost": cost_i,
                "bucket": key,
                "in_flight_cost": st.in_flight_cost,
                "capacity": cap,
            }
            self._idempo_put(st, fp, {"_status": self.policy.http.accepted, **payload})
            return self.policy.http.accepted, payload

    async def _release_later(self, key: str, order_id: str, delay_s: float) -> None:
        await asyncio.sleep(max(0.0, delay_s))
        async with self._lock:
            st = self._state_for(key)
            rec = st.in_flight.pop(order_id, None)
            if not rec:
                return
            st.in_flight_cost = max(0, st.in_flight_cost - rec.cost)
            rec.served_ms = now_ms()
            rec.state = "served"
            st.served.append(rec)

    async def status(self) -> Dict[str, Any]:
        async with self._lock:
            shared = self._state_for("shared")
            by_bucket: Dict[str, Any] = {}
            total_in_flight = 0
            total_served = 0
            served_orders = []
            for key, st in self._states.items():
                total_in_flight += st.in_flight_cost
                total_served += len(st.served)
                by_bucket[key] = {
                    "inFlightCost": st.in_flight_cost,
                    "inFlightCount": len(st.in_flight),
                    "servedCount": len(st.served),
                }
                served_orders.extend([
                    {
                        "orderId": rec.order_id,
                        "bucket": rec.customer_key,
                        "cost": rec.cost,
                        "state": rec.state,
                        "servedMs": rec.served_ms,
                    }
                    for rec in st.served[-50:]
                ])
            return {
                "inFlightCost": total_in_flight,
                "servedCount": total_served,
                "servedOrders": served_orders,
                "uniqueCustomers": sorted(list(shared.unique_customers)) if shared.unique_customers else sorted(list({k for k in by_bucket.keys() if k != 'shared'})),
                "byBucket": by_bucket,
            }
