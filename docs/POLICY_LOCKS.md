# Capax Policy Locks

This document defines the locked behavior for Capax v2.

The goal is to keep policies:
- readable
- deterministic
- explainable
- testable
- aligned with business value

Capax exists to **save query cost** by stopping expensive API work before it starts.

---

## 1. Endpoint mapping is explicit

Capax works with at least two routes:

### Protect endpoint
This is the endpoint where expensive work tries to enter.

Examples:
- `POST /order`
- `POST /bonus/eligibility`
- `POST /graphql`

Capax verb:
- `protect`

### Observe endpoint
This is the endpoint QA uses to inspect resulting state and lifecycle.

Examples:
- `GET /status`
- `GET /bonus/status`

Capax verb:
- `observe`

These two routes must be configured explicitly.

---

## 2. Cost-driving field is explicit

Capax must ask the user to choose which request field changes cost.

A field changes cost if different values cause:
- more database work
- more vendor/API calls
- more compute
- more data to scan
- more downstream fanout
- longer occupation of the workload budget

Capax does not guess this silently.

---

## 3. Value-based scoring is case-insensitive

When a field uses value-based scoring, Capax matches values case-insensitively.

Examples:
- `beer`
- `BEER`
- `Beer`
- `bEeR`

All of these must map to the same score unless the policy explicitly says otherwise.

Reason:
Real clients often send inconsistent casing.
Case drift must not silently create different cost classes.

---

## 4. Workload ladder lock

Default workload ladder:

- cheap = 1
- normal = 2
- expensive = 4
- very expensive = 8

The selected value in the chosen field maps to one of these scores.

This score is the request weight used in admission control.

Example:
- `drinkType = beer` -> 1
- `drinkType = non-beer` -> 2 or 4 depending on the chosen ladder class

---

## 5. Admission rule lock

Admission behavior is deterministic:

admit if:

`current_workload + request_weight <= safe_budget`

reject otherwise.

Default rejection at capacity:
- `429`

---

## 6. Validation behavior lock

### Missing cost-driving field
Default:
- reject with `400`

Configurable:
- reject with another chosen validation status
- fallback to default cost

### Unknown value in cost-driving field
Default:
- reject with `400`

Configurable:
- reject with another chosen validation status
- fallback to default cost

Capax must not claim that `400` was required by the original assignment.
It is the default locked behavior for safety and clarity, but it remains configurable.

---

## 7. Fairness lock

If fairness is enabled, one actor gets one capacity bucket.

Typical fairness fields:
- accountId
- customerId
- tenantId
- apiKey
- IP address

Reason:
One actor should not be allowed to monopolize the whole system.

---

## 8. Idempotency lock

If idempotency is enabled:
- immediate duplicate retries must not silently double-charge hidden capacity
- duplicate handling must be part of the QA suite

Reason:
Retries are one of the fastest ways to multiply real infrastructure cost.

---

## 9. Observe contract lock

The observe endpoint must be usable by QA to inspect lifecycle state.

Minimum expectations:
- endpoint is reachable
- lifecycle can be inspected after accept
- lifecycle can be inspected after release timing

Preferred status payload fields:
- current in-flight cost
- served count
- served list or equivalent summary

Capax should tolerate different field names when parsing observe responses, but the payload should remain coherent.

---

## 10. Runtime lock

Capax may run in:
- reference Python engine
- generated Node runtime
- generated Python runtime
- generated Rust runtime

The selected runtime must be the one actually used for local QA when the user chooses it.

If the reference engine is used instead, the CLI must say so explicitly.

---

## 11. QA lock

Capax must auto-generate QA scenarios from:
- business flow
- protect endpoint
- observe endpoint
- selected cost-driving field
- fairness field
- safe workload budget
- validation behavior
- idempotency
- lifecycle timing

Capax must bias the QA suite toward tricky edge cases:
- exact thresholds
- overflow by one
- case variants
- unknown values
- missing fields
- mixed workloads
- fairness boundaries
- retries
- observe timing

Capax must also isolate scenario execution so one scenario does not silently poison the next one.

---

## 12. Fresh-start lock

After a QA run against a local server started by Capax, the CLI should offer to:
- stop the local server
- clear in-memory state
- start fresh later

This is especially important for timer-based in-memory models.

---

## 13. Readability lock

Every Capax policy must still be explainable to a product manager in under two minutes.

If a policy becomes too flexible to explain, it has lost the main product advantage.

Capax is not “infinite rule power”.

Capax is **readable query-cost control**.
