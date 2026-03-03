from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Policy, CostExpression, Isolation, HttpCodes, Idempotency, save_yaml, compile_policy
from .utils import infer_fields, pretty_json
from .generator import generate_runtime


def _prompt(msg: str, default: Optional[str] = None) -> str:
    if default is not None:
        s = input(f"{msg} [Default: {default}]\n> ").strip()
        return s if s else default
    return input(f"{msg}\n> ").strip()


def _prompt_choice(msg: str, choices: Dict[str, str], default_key: str) -> str:
    print(msg)
    for k, v in choices.items():
        print(f"{k}) {v}")
    s = _prompt(f"Pick {min(choices.keys())}-{max(choices.keys())}", default_key)
    return s if s in choices else default_key


def _prompt_yesno(msg: str, default_no: bool = True) -> bool:
    default = "N" if default_no else "Y"
    s = _prompt(f"{msg} (y/N)" if default_no else f"{msg} (Y/n)", default)
    return s.strip().lower().startswith("y")


def _parse_endpoint(s: str) -> tuple[str, str]:
    parts = s.strip().split()
    if len(parts) == 1:
        return "POST", parts[0]
    if len(parts) >= 2:
        return parts[0].upper(), parts[1]
    return "POST", "/order"


def _read_oneline_json_object(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        return None
    except Exception:
        return None


def run_wizard(project_root: Path) -> None:
    print("Capax — query admission control (weighted concurrency)")
    print("You will define one endpoint, one sample request, and capacity math.")
    print('Tip: paste request body as a ONE-LINE JSON string. Example: {"id":1,"type":"A"}')
    input("Press Enter to begin.\n")

    pack = _prompt("Give this ruleset a name (letters/numbers).", "my_rules")

    endpoint_line = _prompt("Which HTTP endpoint do you want to protect?\nEnter: METHOD /path", "POST /order")
    method, path = _parse_endpoint(endpoint_line)

    print("\nPaste a ONE-LINE sample JSON request body (must be an object).")
    print('Example: {"customerId":1,"drinkType":"BEER"}')
    print('If you have schema-like options, keep it valid JSON: "BEER | DRINK" (a single string).')
    body_line = _prompt("Paste ONE-LINE JSON now (or press Enter to use the example):", "")
    if body_line.strip():
        sample = _read_oneline_json_object(body_line.strip())
        if sample is None:
            print("That was not valid one-line JSON object. Using the example instead.")
            sample = {"customerId": 1, "drinkType": "BEER"}
    else:
        sample = {"customerId": 1, "drinkType": "BEER"}

    print("\nThis is the request I will control:")
    print(f"{method} {path}")
    print(pretty_json(sample))

    fields = infer_fields(sample)
    print("\nDetected fields:")
    for f in fields:
        print(f"- {f.name}: {f.type}")

    # Step: pick weight field (or none)
    if fields:
        opts = {str(i + 1): f.name for i, f in enumerate(fields)}
        opts[str(len(fields) + 1)] = "none (all requests cost 1 slot)"
        pick = _prompt_choice("\nWhich field decides how many slots a request consumes?", opts, "1")
        weight_field = None if pick == str(len(fields) + 1) else opts[pick]
    else:
        weight_field = None

    # Slots first, for clarity
    print("\nMax allowed concurrent requests (in slots)")
    print("Rule: accept if sum(in-flight slots) + request_slots <= max_slots.")
    print("Example: max_slots=2 and request_slots=1 -> 2 can run together.")
    print("Example: max_slots=2 and request_slots=2 -> only 1 can run at a time.")
    max_slots = int(_prompt("Set max_slots (integer)", "2"))

    # Weight mapping
    cost_expr = CostExpression(field=None)
    if weight_field:
        cost_expr.field = weight_field
        use_weights = _prompt_yesno(
            f"Does '{weight_field}' value change the request weight (slots) depending on input?",
            default_no=False
        )
        if use_weights:
            print("\nDefine weight per value (case-insensitive).")
            print("Example: BEER -> 1, DRINK -> 2")
            token_costs: Dict[str, int] = {}
            default_value = str(sample.get(weight_field, "") or "")
            while True:
                v = _prompt("Enter a value (blank to finish):", default_value if not token_costs and default_value else "")
                if not v.strip():
                    break
                c = int(_prompt(f"Weight (slots) for '{v}'", "1"))
                token_costs[v] = c
                default_value = ""
            cost_expr.token_costs = token_costs

            unknown_action = _prompt_choice(
                "\nIf an unknown value appears, what should happen?",
                {"1": "treat it as weight 1", "2": "reject with 400"},
                "1",
            )
            if unknown_action == "2":
                cost_expr.unknown_action = "reject_400"
            else:
                cost_expr.unknown_action = "default_cost"
                cost_expr.unknown_default_cost = 1

    duration_s = float(_prompt("\nHow long does an accepted request hold slots if nothing tells us it finished? (seconds)", "5"))

    # Isolation
    iso_mode = _prompt_choice(
        "\nShould this limit be shared by everyone, or separated per a key (like per customer)?",
        {"1": "Shared limit", "2": "Separate limit by one field"},
        "1"
    )
    isolation = Isolation(enabled=False, field=None)
    if iso_mode == "2":
        isolation.enabled = True
        if fields:
            opts = {str(i + 1): f.name for i, f in enumerate(fields)}
            pick = _prompt_choice("Which field identifies the key?", opts, "1")
            isolation.field = opts[pick]

    status_path = _prompt("\nStatus endpoint path (optional)", "/status")

    # HTTP codes (forceable, per assignment if desired)
    accepted_code = int(_prompt("\nHTTP status code for accepted/admitted", "200"))
    capacity_code = int(_prompt("HTTP status code for at-capacity", "429"))

    # Idempotency
    idem_on = _prompt_yesno("\nEnable idempotency (duplicate submissions should not duplicate serving)?", default_no=True)
    idem_ttl = 600
    if idem_on:
        idem_ttl = int(_prompt("Idempotency TTL seconds", "600"))

    policy = Policy(
        pack_name=pack,
        endpoint_method=method,
        endpoint_path=path,
        status_path=status_path,
        max_allowed_concurrent_capacity=max_slots,
        duration_seconds=duration_s,
        cost=cost_expr,
        isolation=isolation,
        http=HttpCodes(accepted=accepted_code, at_capacity=capacity_code),
        idempotency=Idempotency(enabled=idem_on, ttl_seconds=idem_ttl),
    )

    # Preview
    print("\nPreview:")
    print(f"- max_slots = {policy.max_allowed_concurrent_capacity}")
    if policy.cost.field and policy.cost.token_costs:
        print(f"- weight field = {policy.cost.field} (case-insensitive mapping)")
        for k, v in policy.cost.token_costs.items():
            mx = max(1, policy.max_allowed_concurrent_capacity // max(1, int(v)))
            print(f"  value '{k}' -> weight {v} => up to {mx} concurrent")
    else:
        print("- weight = 1 for every request")

    if not _prompt_yesno("\nDoes this look correct?", default_no=False):
        print("Cancelled.")
        return

    # Write pack files
    pack_dir = project_root / "packs" / pack
    pack_dir.mkdir(parents=True, exist_ok=True)
    policy_yaml = pack_dir / "policy.yaml"
    sample_json = pack_dir / "sample_request.json"

    from .config import policy_to_dict
    save_yaml(policy_yaml, policy_to_dict(policy))
    sample_json.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")

    # Registry
    server_dir = project_root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    registry = server_dir / "registry.yaml"
    registry.write_text(
        "\n".join([
            "routes:",
            f"  - kind: admit",
            f"    pack: {pack}",
            f"    method: {method}",
            f"    path: {path}",
            f"    policy_yaml: packs/{pack}/policy.yaml",
            f"  - kind: status",
            f"    pack: {pack}",
            f"    method: GET",
            f"    path: {status_path}",
            f"    policy_yaml: packs/{pack}/policy.yaml",
            "",
        ]),
        encoding="utf-8"
    )

    # Compile now
    compiled_json = pack_dir / "compiled_policy.json"
    compile_policy(policy_yaml, compiled_json)

    # Runtime selection + port
    lang = _prompt_choice("\nWhich runtime do you want to generate?", {"1": "python", "2": "node", "3": "rust (stub)"}, "2")
    lang_name = {"1": "python", "2": "node", "3": "rust"}[lang]
    port = int(_prompt("Port for the generated server", "8080"))

    out_dir = generate_runtime(project_root, pack, lang_name)

    print("\nGenerated:")
    print(f"- {policy_yaml}")
    print(f"- {sample_json}")
    print(f"- {compiled_json}")
    print(f"- {registry}")
    print(f"- runtime: {out_dir}")

    # Print ONE run command depending on runtime
    print("\nNext:")
    if lang_name == "python":
        print(f"capax run --registry server/registry.yaml --host 127.0.0.1 --port {port}")
    elif lang_name == "node":
        print(f"cd gen/{pack}/node && PORT={port} node server.js")
    else:
        print(f"Rust stub generated at: gen/{pack}/rust (implement server using compiled_policy.json)")

    print(f"capax test --pack {pack} --http http://127.0.0.1:{port}")
