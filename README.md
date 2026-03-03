<p align="center">
  <img src="assets/capax-logo.svg" alt="Capax" width="160" />
</p>

# Capax
**Spec-to-code query admission control for HTTP APIs.**

Capax sits in front of an endpoint and decides in constant time whether a request can **start now**:

- **Admit**: allow the expensive work to begin
- **Reject fast** (default 429): do not start work, do not burn CPU, DB, or vendor credits

This is not about “requests per minute”, but about **in-flight work**: the only number that matters when systems approach saturation.

---

## Why this exists (origin story)
Capax started from a challenge: build a small HTTP API with a strict concurrency limit and correct status reporting. The requirements looked simple, I was ready to code a simple solution, but digging deeper, I figured out the real problem was hidden in admission timing:

- you must decide **before** starting work
- because once work starts, cost is already incurred
- and when latency rises, retries multiply and load spirals

That was the insight: **a correct early admission decision is a direct money lever**.

---

## Financial value: fail early, not late
Most query protections have a financial leak because they fail late.

Capax is designed to fail early.

When load rises, retries and queue growth can create positive feedback loops. Near saturation, they multiply in-flight work and blow up tail latency, turning small spikes into incidents with real cost.

A simple waste model:
```
waste =
  overload_requests × unit_cost_per_request
+ timeouts × (retry_multiplier × unit_cost + support_cost)
+ incident_hours × burn_rate
```

Rejected requests are cheap. Timeouts are expensive.

---

## What Capax is
Capax enforces a **hard in-flight budget**, optionally **query-weighted**.

You define:
- `max_allowed_concurrent_capacity`: total slot budget
- `request_slots`: how many slots a request consumes (integer >= 1)

Decision:
- admit if `in_flight_slots + request_slots <= max_allowed_concurrent_capacity`
- otherwise reject fast (default 429)

### Query weighting (payload drives cost)
Many endpoints are not “one request = one cost”. Capax can map a JSON field value to a weight, **case-insensitive** (values are compared in lowercase via normalization).

Example:
- `drinktype=beer` consumes 1 slot
- `drinktype=cocktail` consumes 3 slots
- `drinktype=champagne` consumes 5 slots

Same idea applies to real “queries”:
- GraphQL: depth, field set, complexity class
- SQL-submit endpoints: query class, expected scan bucket, tenant tier
- LLM endpoints: model, max_tokens, tools used

---

## Why it is different vs Athena, Synapse, Power BI, and typical “capacity” tools
Capax is an **admission layer**. It answers: “should this query start now?”

That is different from query engines and analytics platforms:

- **Athena / Synapse**: execute and scale queries once submitted
- **Power BI / Fabric capacity**: monitor and manage analytics capacity
- **Typical rate limiters**: control frequency, not in-flight work
- **Queues**: buffer spikes into backlog (latency + timeouts + retries)

Capax stops the cost **before** it begins.

---

## Config-first by design: policy packs
Capax is mostly configuration.

A pack typically contains:
- `policy.yaml` (concurrency budget + weight rules)
- `sample_request.json` (used for QA + codegen)
- `compiled_policy.json` (generated)
- `server/registry.yaml` (optional multi-route registry)

The policy pack is the product:
- versioned
- reproducible
- portable across environments

---

## Built-in QA: conformance derived from policy
If you cannot prove concurrency behavior, you do not own it.

Capax generates and runs conformance scenarios from the policy:
- fill capacity with the cheapest request, then assert overflow returns 429
- mixed weights at the boundary (heavy + cheap)
- status endpoint reflects served orders after completion model
- case-insensitive value matching behaves consistently
- idempotency (when enabled) does not double-consume slots under retries

---

## Code generation: Node.js / Python / Rust
To avoid rewriting guardrails in each stack, Capax can generate runtime scaffolds from the same policy pack:

- Node.js
- Python
- Rust (scaffold skeleton)

```
capax generate --pack <your_pack> --lang node
capax generate --pack <your_pack> --lang python
capax generate --pack <your_pack> --lang rust
```

---

## Quickstart
### 1) Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2) Create a policy pack (wizard)
```bash
capax wizard
```

### 3) Run the capax server
```bash
capax run --host 127.0.0.1 --port 8080
```

### 4) Run QA derived from the policy
```bash
capax test --pack <your_pack> --http http://127.0.0.1:8080
```

---

## Learn more
Deep dive articles and design notes:
- https://medium.com/@edouard-kombo/capax-stop-costly-queries-before-they-start-concurrency-control-framework-yaml-based-e8292ccd349b

---
