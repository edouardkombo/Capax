# Capax QA Matrix

Capax auto-generates a QA suite from the selected business flow, the protected endpoint, the observe endpoint, the chosen cost-driving field, the safe workload budget, the fairness field, and idempotency settings.

The goal is not only to test the happy path.

Capax intentionally biases the QA suite toward edge conditions where cost-control policies usually fail:
- threshold boundaries
- mixed workloads
- case variants
- unexpected values
- missing fields
- fairness / monopolization attempts
- retries
- lifecycle timing

It also isolates scenarios so that one scenario does not silently poison the next one.

---

## Core principle

Capax validates **query-cost admission behavior**, not only request-rate behavior.

Each scenario checks that the configured request values, case handling, validation policy, fairness bucket, and release lifecycle behave exactly as intended.

---

## Scenario isolation lock

Capax must not let leftover in-flight state from one QA scenario break later scenarios.

So QA execution must:
- use unique actor bucket values per scenario when fairness is enabled
- use unique request nonces per scenario
- drain timer-based in-flight state between scenarios
- optionally let the user stop the local server after QA to start fresh

Why it matters:
Without isolation, a passing gate can look broken simply because one earlier scenario kept memory hot.

---

## Scenario families

### 1. Capacity boundary scenarios
These prove the hard workload budget is real.

Examples:
- cheapest request accepts
- cheap requests fill the budget exactly
- one more cheap request overflows
- more than expected max concurrency is rejected

Generated when:
- always

Why it matters:
A cost gate that cannot hold a strict threshold is not a cost gate.

---

### 2. Case-insensitive value scenarios
These prove that request values are matched case-insensitively.

Examples:
- `beer`
- `BEER`
- `Beer`
- `bEeR`

Generated when:
- the selected cost-driving field uses value-based scoring

Why it matters:
Real clients do not always send consistent casing.
Capax should not silently treat case variants as different cost classes unless explicitly configured otherwise.

---

### 3. Unexpected value scenarios
These test values that were not declared in the policy.

Examples:
- `__CAPAX_UNEXPECTED__`
- unknown model name
- unknown query class

Generated when:
- the selected cost-driving field uses value-based scoring

Expected behavior:
- reject with the configured validation status, or
- accept with the configured fallback default cost

Why it matters:
Unknown values are common in production.
The policy must remain safe when reality changes.

---

### 4. Missing field scenarios
These test requests where the scoring field is absent.

Examples:
- missing `drinkType`
- missing `model`
- missing `queryClass`

Generated when:
- always for selected cost-driving fields

Expected behavior:
- reject with the configured validation status, or
- accept with the configured fallback default cost

Why it matters:
A missing scoring field is a classic edge case.
Capax must define what to do instead of guessing.

---

### 5. Heavy request scenarios
These prove that cost classes actually change admission behavior.

Examples:
- heaviest request alone
- heavy then cheap
- cheap then heavy
- heavy request overflows after threshold fill

Generated when:
- more than one cost class exists

Why it matters:
Without this family, scoring could be present in configuration but meaningless in runtime.

---

### 6. Mixed workload scenarios
These test order-sensitive combinations around the budget threshold.

Examples:
- cheap then heavy
- heavy then cheap
- exact threshold equality
- just-over-threshold overflow

Generated when:
- more than one cost class exists

Why it matters:
Policies often pass pure cheap-only or heavy-only tests but fail mixed transitions.

---

### 7. Fairness / isolation scenarios
These verify that one actor cannot monopolize the system.

Examples:
- same actor fills their bucket
- same actor overflows
- different actor still has separate capacity

Generated when:
- fairness / isolation is enabled

Why it matters:
This is crucial for:
- bonus abusers
- noisy tenants
- abusive API keys
- one customer dominating shared infrastructure

---

### 8. Retry / idempotency scenarios
These verify that duplicate retries do not silently double-charge hidden capacity.

Examples:
- immediate retry after accept
- duplicate request replay with same body
- exact in-flight delta remains bounded

Generated when:
- idempotency is enabled

Why it matters:
Retries are one of the fastest ways to turn a small spike into real cost.

---

### 9. Observe / lifecycle scenarios
These use the observe endpoint to verify that work moves through its lifecycle coherently.

Examples:
- observe endpoint is reachable immediately after accept
- observe endpoint reflects work after release window
- served count becomes visible after the configured hold time

Generated when:
- an observe endpoint is configured

Why it matters:
A gate that admits and rejects correctly but never releases budget will eventually deadlock itself.

---

## Generation rules

Capax derives scenarios automatically using these rules.

### If there is only one cost class
Generate:
- cheapest accepts
- threshold fill
- overflow by one
- observe-after-release
- case variants
- unexpected value
- missing field

### If there are multiple cost classes
Generate everything above plus:
- heavy alone
- heavy then cheap
- cheap then heavy
- mixed threshold scenarios

### If fairness is enabled
Generate:
- same actor fills bucket
- same actor overflows
- different actor still accepts

### If idempotency is enabled
Generate:
- duplicate retry replay
- exact in-flight delta bounded after duplicate

### If an observe endpoint exists
Generate:
- immediate observe reachability
- delayed observe after release window

---

## Validation behavior lock

For selected cost-driving fields:

### Missing field
Default:
- reject with validation status `400`

Extensible options:
- reject with a different configured validation status
- fallback to default cost

### Unknown value
Default:
- reject with validation status `400`

Extensible options:
- reject with a different configured validation status
- fallback to default cost

---

## QA run fresh-start behavior

After running QA against a local server started by Capax, the CLI should offer to:
- stop the local server
- clear in-memory state
- start again fresh later

This is especially useful for timer-based release models.

---

## Expected QA report behavior

A QA report must include:
- scenario count
- pass count
- fail count
- base URL
- failing scenarios with reasons

This keeps Capax observable and replayable.

---

## Philosophy

Capax QA is not only regression protection.

It is a way to prove that:
- expensive values really cost more
- weird casing does not bypass the scorer
- missing fields do not create silent policy holes
- unknown values follow the chosen validation policy
- one actor cannot monopolize capacity
- retries do not secretly multiply cost
- lifecycle timing really releases budget
