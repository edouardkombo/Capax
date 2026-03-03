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
        # Build a case-insensitive lookup once.
        self._token_costs_ci: Dict[str, int] = {str(k).casefold(): int(v) for k, v in (policy.cost.token_costs or {}).items()}

    def _get_isolation_key(self, body: Dict[str, Any]) -> str:
        iso = self.policy.isolation
        if not iso.enabled or not iso.field:
            return "shared"
        v = body.get(iso.field)
        return f"{iso.field}={v}"

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
        # 1) exact match
        m = self.policy.cost.token_costs or {}
        if tok in m:
            return int(m[tok]), None
        # 2) case-insensitive match
        tcf = tok.casefold()
        if tcf in self._token_costs_ci:
            return int(self._token_costs_ci[tcf]), None
        # 3) unknown
        if self.policy.cost.unknown_action == "reject_400":
            return None, f"unknown_token:{tok}"
        return int(self.policy.cost.unknown_default_cost), None

    def compute_cost(self, body: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
        field = self.policy.cost.field
        if not field:
            return 1, None
        raw = body.get(field)
        if raw is None:
            return 1, None

        # Default: single value. We still SUPPORT multi-token strings like "A | B"
        # for teams that receive messy payloads, but the CLI does not emphasize it.
        if not isinstance(raw, str):
            return self._token_cost(str(raw))
        raw_s = raw.strip()
        if raw_s == "":
            return 1, None

        tokens = self._split_tokens(raw_s)
        costs: List[int] = []
        for tok in tokens:
            c, err = self._token_cost(tok)
            if err:
                return None, err
            costs.append(int(c))  # type: ignore

        if not costs:
            return 1, None
        if len(costs) == 1:
            return costs[0], None

        if self.policy.cost.combine_mode == "bundle_sum":
            return sum(costs), None
        return max(costs), None

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
                return status, {k: v for k, v in cached.items() if k != "_status"}

            cost, err = self.compute_cost(body)
            if err:
                payload = {"result": "rejected", "reason": err, "customerKey": key, "_status": 400}
                self._idempo_put(st, fp, payload)
                return 400, {k: v for k, v in payload.items() if k != "_status"}

            assert cost is not None
            if st.in_flight_cost + int(cost) > self.policy.max_allowed_concurrent_capacity:
                payload = {"result": "rejected", "reason": "at_capacity", "customerKey": key, "_status": self.policy.http.at_capacity}
                self._idempo_put(st, fp, payload)
                return self.policy.http.at_capacity, {k: v for k, v in payload.items() if k != "_status"}

            order_id = fp[:12]
            rec = OrderRecord(order_id=order_id, customer_key=key, body=body, cost=int(cost), admitted_ms=now_ms())
            st.in_flight[order_id] = rec
            st.in_flight_cost += int(cost)

            payload = {"result": "accepted", "orderId": order_id, "customerKey": key, "cost": int(cost), "_status": self.policy.http.accepted}
            self._idempo_put(st, fp, payload)

            asyncio.create_task(self._complete_after(key, order_id, float(self.policy.duration_seconds)))
            return self.policy.http.accepted, {k: v for k, v in payload.items() if k != "_status"}

    async def _complete_after(self, key: str, order_id: str, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
        st = self._state_for(key)
        async with self._lock:
            rec = st.in_flight.pop(order_id, None)
            if not rec:
                return
            rec.state = "served"
            rec.served_ms = now_ms()
            st.in_flight_cost -= rec.cost
            st.served.append(rec)

            iso_field = self.policy.isolation.field if self.policy.isolation.enabled else None
            if iso_field and iso_field in rec.body:
                st.unique_customers.add(str(rec.body.get(iso_field)))
            else:
                st.unique_customers.add(rec.customer_key)

    async def status(self) -> Dict[str, Any]:
        async with self._lock:
            served_orders = []
            unique_customers = set()
            for st in self._states.values():
                for rec in st.served:
                    served_orders.append({
                        "orderId": rec.order_id,
                        "customerKey": rec.customer_key,
                        "body": rec.body,
                        "admittedMs": rec.admitted_ms,
                        "servedMs": rec.served_ms,
                        "cost": rec.cost,
                    })
                unique_customers |= set(st.unique_customers)
            return {"servedOrders": served_orders, "uniqueCustomers": sorted(list(unique_customers))}
