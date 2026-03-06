from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    CostExpression,
    HttpCodes,
    Idempotency,
    Isolation,
    Policy,
    QaSettings,
    compile_policy,
    policy_to_authoring_dict,
    policy_to_dict,
    save_yaml,
)
from .generator import generate_runtime
from .qa import generate_scenarios, run_scenarios, save_report, save_scenarios
from .utils import infer_fields, pretty_json

BUSINESS_CASES = {
    "1": "bonus / promotion eligibility",
    "2": "kyc / fraud / verification",
    "3": "ai / llm generation",
    "4": "graphql / search / query endpoint",
    "5": "checkout / payment risk checks",
    "6": "generic api endpoint",
    "7": "bartender tutorial demo",
}

HTTP_METHODS = {
    "1": "GET",
    "2": "POST",
    "3": "PUT",
    "4": "PATCH",
    "5": "DELETE",
}

SEP = "\n" + "=" * 72 + "\n"


def _say(msg: str) -> None:
    print(msg)


def _section(title: str) -> None:
    _say(SEP + title + "\n")


def _prompt(msg: str, default: Optional[str] = None) -> str:
    suffix = f" [default: {default}]" if default is not None else ""
    s = input(f"{msg}{suffix}\n> ").strip()
    return s if s else (default or "")


def _prompt_int(msg: str, default: int) -> int:
    while True:
        s = _prompt(msg, str(default))
        try:
            return int(s)
        except ValueError:
            _say("Please enter a whole number.")


def _prompt_float(msg: str, default: float) -> float:
    while True:
        s = _prompt(msg, str(default))
        try:
            return float(s)
        except ValueError:
            _say("Please enter a number.")


def _prompt_json(msg: str) -> Dict[str, Any]:
    while True:
        s = _prompt(msg)
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        _say("That was not valid one-line JSON. Please paste a single JSON object.")


def _prompt_choice(msg: str, choices: Dict[str, str], default_key: str) -> str:
    _say(msg)
    for k, v in choices.items():
        _say(f"[{k}] {v}")
    s = _prompt("Choice:", default_key)
    return s if s in choices else default_key


def _prompt_yesno(msg: str, default_yes: bool = True) -> bool:
    default = "y" if default_yes else "n"
    s = _prompt(f"{msg} ({'Y/n' if default_yes else 'y/N'})", default).lower()
    return s.startswith("y")


def _show_cmd(cmd: List[str], extra: str = "") -> None:
    _say("Executing command:")
    _say(" ".join(cmd) + (f" {extra}" if extra else ""))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _normalize_path(path: str) -> str:
    path = path.strip()
    if not path:
        return "/"
    return path if path.startswith("/") else "/" + path


def _runtime_run_spec(
    project_root: Path,
    pack: str,
    lang: str,
    framework: str,
    port: int,
) -> Tuple[Path, Dict[str, str], List[List[str]], List[str]]:
    env = {
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "CAPAX_ADDR": f"127.0.0.1:{port}",
        "CAPAX_ROOT": str(project_root),
        "CAPAX_POLICY_PATH": "compiled_policy.json",
    }

    if lang == "reference":
        cwd = project_root
        prep: List[List[str]] = []
        cmd = [
            "python",
            "-m",
            "capax",
            "run",
            "--registry",
            "server/registry.yaml",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        return cwd, env, prep, cmd

    if lang == "python":
        cwd = project_root / "gen" / pack / "python" / "fastapi"
        prep = []
        cmd = ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)]
        return cwd, env, prep, cmd

    if lang == "node":
        cwd = project_root / "gen" / pack / "node" / framework
        prep = [["npm", "install"]]
        cmd = ["npm", "start"]
        return cwd, env, prep, cmd

    if lang == "rust":
        cwd = project_root / "gen" / pack / "rust" / "axum"
        prep = []
        cmd = ["cargo", "run"]
        return cwd, env, prep, cmd

    raise ValueError(f"Unsupported runtime: {lang}/{framework}")


def _start_runtime_background(
    project_root: Path,
    pack: str,
    lang: str,
    framework: str,
    port: int,
    log_path: Path,
):
    cwd, env, prep_cmds, cmd = _runtime_run_spec(project_root, pack, lang, framework, port)
    merged_env = {**os.environ.copy(), **env}

    for prep in prep_cmds:
        _show_cmd(prep)
        subprocess.run(prep, cwd=str(cwd), env=merged_env, check=True)

    _show_cmd(cmd, f"> {log_path} 2>&1 &")
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=merged_env, stdout=logf, stderr=logf)
    return proc, cwd, cmd


def _classify_values(field: str, sample: Dict[str, Any]) -> Dict[str, int]:
    _say(f"Field selected: {field}\n")
    _say(
        "Why we ask:\n"
        "Capax scores the VALUE that will be filled into this field. "
        "Each value is mapped to the workload ladder below.\n"
    )
    _say("Workload ladder:\n• cheap = 1\n• normal = 2\n• expensive = 4\n• very expensive = 8\n")
    _say(
        "Important:\n"
        "• matching is case-insensitive\n"
        "• Capax lowercases values internally before scoring\n"
        "• the final request weight is taken from the value placed in this field\n"
    )
    _say(
        "What Capax will do with your answer:\n"
        "• build a value-to-score map for this field\n"
        "• use those exact weights in admission and QA scenarios\n"
    )

    sample_default = str(sample.get(field, ""))
    cheap = [v.strip() for v in _prompt("Enter CHEAP values separated by commas.", sample_default).split(",") if v.strip()]
    normal = [v.strip() for v in _prompt("Enter NORMAL values separated by commas.", "").split(",") if v.strip()]
    expensive = [v.strip() for v in _prompt("Enter EXPENSIVE values separated by commas.", "").split(",") if v.strip()]
    very_expensive = [v.strip() for v in _prompt("Enter VERY EXPENSIVE values separated by commas.", "").split(",") if v.strip()]

    token_costs: Dict[str, int] = {}
    for v in cheap:
        token_costs[v.casefold()] = 1
    for v in normal:
        token_costs[v.casefold()] = 2
    for v in expensive:
        token_costs[v.casefold()] = 4
    for v in very_expensive:
        token_costs[v.casefold()] = 8

    if not token_costs:
        token_costs[sample_default.casefold() or "default"] = 1

    return token_costs


def _stop_local_process(proc: subprocess.Popen, pid_file: Path) -> None:
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def run_wizard(project_root: Path) -> None:
    _say("Welcome to Capax.\n")
    _say("Capax saves query cost by stopping expensive API work before it starts.")
    _say("It protects APIs from costly requests by measuring query weight and enforcing a safe workload budget before work begins.\n")
    _say(
        "In this tutorial we will:\n"
        "1. choose the business flow to protect\n"
        "2. define the protected and status endpoints\n"
        "3. inspect a real request\n"
        "4. identify which request fields increase cost\n"
        "5. define the workload scoring\n"
        "6. decide who gets a fair share of capacity\n"
        "7. define the safe workload budget\n"
        "8. preview QA scenarios\n"
        "9. generate runtime code\n"
        "10. choose a port and start the server\n"
        "11. optionally run QA scenarios and clear memory for manual retesting\n"
    )

    step = 1
    _section(f"Step {step} of 11 — What are you trying to protect?")
    choice = _prompt_choice(
        "Why we ask:\n"
        "Different business flows become expensive in different ways.\n\n"
        "What Capax will do with your answer:\n"
        "• tailor the prompts\n"
        "• suggest likely expensive request patterns\n"
        "• bias the QA suite toward tricky edge cases\n",
        BUSINESS_CASES,
        "7",
    )
    flow = BUSINESS_CASES[choice]
    pack = _prompt(
        "Give this ruleset a short name (letters/numbers/underscores).",
        flow.replace(" ", "_").replace("/", "_").replace("-", "_")[:24],
    )

    step += 1
    _section(f"Step {step} of 11 — Which API routes are involved?")
    _say(
        "Capax works with at least two routes:\n"
        "• a protected endpoint where costly requests enter\n"
        "• a status endpoint where QA scenarios verify what happened\n"
    )
    _say(
        "What Capax will do with your answer:\n"
        "• map the protected endpoint to the verb 'protect'\n"
        "• map the status endpoint to the verb 'observe'\n"
        "• generate QA scenarios against both\n"
    )
    default_protect = "/order" if choice == "7" else "/query"

    _say("For endpoint methods, choose from standard HTTP verbs below. This prevents invalid values like typing 'order' instead of POST.\n")
    protect_method_key = _prompt_choice("Protected endpoint method:", HTTP_METHODS, "2")
    protect_method = HTTP_METHODS[protect_method_key]

    _say("For endpoint paths, Capax expects a leading '/'. You can type 'order' or '/order' and Capax will normalize it for you.\n")
    protect_path = _normalize_path(_prompt("Protected endpoint path:", default_protect))

    status_method_key = _prompt_choice("Status endpoint method:", HTTP_METHODS, "1")
    status_method = HTTP_METHODS[status_method_key]
    status_path = _normalize_path(_prompt("Status endpoint path:", "/status"))

    _say(f"Capax endpoint map\nprotect -> {protect_method} {protect_path}\nobserve -> {status_method} {status_path}")

    step += 1
    _section(f"Step {step} of 11 — Show Capax a real request for the protected endpoint")
    sample_default = {"customerId": "cust_001", "drinkType": "beer"} if choice == "7" else {"accountId": "acc_001", "queryType": "standard"}
    sample = _prompt_json(
        f"Protected endpoint:\n{protect_method} {protect_path}\n\n"
        "Why we ask:\n"
        "Capax uses the request shape to identify cost-driving fields, fairness fields, and scenario variations for QA.\n\n"
        f"Paste a representative one-line JSON object for this endpoint, for example:\n{json.dumps(sample_default)}"
    )
    _say("This is the request Capax will inspect:")
    _say(pretty_json(sample))

    fields = infer_fields(sample)
    for idx, f in enumerate(fields, 1):
        _say(f"[{idx}] {f.name} ({f.type})")

    step += 1
    _section(f"Step {step} of 11 — Which request fields change cost?")
    _say(
        "A field changes cost if different values cause:\n"
        "• more database work\n"
        "• more vendor/API calls\n"
        "• more compute\n"
        "• more data to scan\n"
        "• more downstream fanout\n"
        "• longer occupation of the workload budget\n"
    )
    _say(
        "What Capax will do with your answer:\n"
        "• build the scoring model from these fields\n"
        "• ignore the others for cost calculation\n"
    )
    field_map = {str(i + 1): f.name for i, f in enumerate(fields)}
    field_default = "2" if choice == "7" and len(fields) >= 2 else "1"
    cost_field_key = _prompt_choice("Select the cost-driving field:", field_map, field_default)
    cost_field = field_map[cost_field_key]

    step += 1
    _section(f"Step {step} of 11 — Review the scoring ladder and classify values")
    token_costs = _classify_values(cost_field, sample)
    _say("Capax proposed this exact value-to-score mapping:")
    for tok, score in sorted(token_costs.items()):
        _say(f"• {tok} -> {score}")

    step += 1
    _section(f"Step {step} of 11 — Who should get a fair share of capacity?")
    _say(
        "Without a fairness field, one actor can consume the whole workload budget.\n\n"
        "Examples:\n"
        "• one gambler spamming bonus checks\n"
        "• one API key sending heavy AI queries\n"
        "• one tenant running huge reports\n"
    )
    fairness_choices = {str(i + 1): f.name for i, f in enumerate(fields)}
    fairness_choices[str(len(fields) + 1)] = "shared global budget only"
    fairness_pick = _prompt_choice(
        "What Capax will do with your answer:\n"
        "• create a separate workload bucket per selected actor\n"
        "• prevent one actor from monopolizing the system\n",
        fairness_choices,
        "1",
    )
    isolation = Isolation(enabled=fairness_pick != str(len(fields) + 1), field=None if fairness_pick == str(len(fields) + 1) else fairness_choices[fairness_pick])

    step += 1
    _section(f"Step {step} of 11 — Define the safe workload budget")
    _say(
        "Think of this as:\n"
        "\"How much total workload can safely run in parallel?\"\n\n"
        "This budget applies across ALL ladder scores together.\n"
        "Examples with budget = 2:\n"
        "• beer(1) + beer(1) = 2 -> allowed\n"
        "• non-beer(2) = 2 -> allowed\n"
        "• beer(1) + non-beer(2) = 3 -> rejected\n"
    )
    cap = _prompt_int("Safe workload budget:", 2 if choice == "7" else 50)
    hold = _prompt_float("How long does accepted work usually occupy the workload budget (seconds)?", 5.0 if choice == "7" else 10.0)

    _prompt_choice(
        "When the workload budget is full, what should happen?\n\n"
        "Why we ask:\n"
        "Immediate rejection is usually safer and cheaper than letting overload become latency and retries.\n",
        {"1": "reject immediately (429)", "2": "dry run only (not yet implemented, still generates 429 policy)"},
        "1",
    )

    bad_request_status = _prompt_int("What status should Capax use for validation rejections (missing/unknown scoring values)?", 400)

    missing_action = _prompt_choice(
        "If the cost-driving field is missing, how should Capax behave?\n\n"
        "Why we ask:\n"
        "Missing fields are a classic edge case. The policy must define whether to reject or charge a fallback cost.\n",
        {"1": f"reject with {bad_request_status}", "2": "accept using default cost 1"},
        "1",
    )

    unknown_action = _prompt_choice(
        "If a new unknown value appears for the cost-driving field, how should Capax behave?\n\n"
        "Why we ask:\n"
        "This keeps the scorer safe when reality changes.\n",
        {"1": f"reject with {bad_request_status}", "2": "accept using default cost 1"},
        "1",
    )

    idem_enabled = _prompt_yesno("Should Capax enable idempotency so immediate duplicate retries do not silently double-charge hidden capacity?", True)
    idem_ttl = _prompt_int("Idempotency TTL in seconds:", 600) if idem_enabled else 600

    policy = Policy(
        pack_name=pack,
        business_flow=flow,
        request_example=sample,
        endpoint_method=protect_method,
        endpoint_path=protect_path,
        status_method=status_method,
        status_path=status_path,
        max_allowed_concurrent_capacity=cap,
        duration_seconds=hold,
        cost=CostExpression(
            field=cost_field,
            token_costs=token_costs,
            unknown_action="reject_400" if unknown_action == "1" else "default_cost",
            unknown_default_cost=1,
            missing_field_action="reject_400" if missing_action == "1" else "default_cost",
            missing_field_default_cost=1,
        ),
        isolation=isolation,
        http=HttpCodes(accepted=200, at_capacity=429, bad_request=bad_request_status),
        idempotency=Idempotency(enabled=idem_enabled, ttl_seconds=idem_ttl),
        qa=QaSettings(
            prefer_tricky=True,
            include_missing_field=True,
            include_unknown_token=True,
            include_fairness=True,
            include_retries=True,
        ),
    )

    step += 1
    _section(f"Step {step} of 11 — Preview QA scenarios")
    scenarios = generate_scenarios(policy_to_dict(policy), sample)
    _say(
        "Capax generated QA scenarios automatically from:\n"
        "• your protected endpoint\n"
        "• your status endpoint\n"
        "• your cost-driving field\n"
        "• your fairness field\n"
        "• your safe workload budget\n"
        "• your hold time\n"
        "• your validation behavior\n"
        "• your idempotency setting\n"
    )
    for idx, sc in enumerate(scenarios, 1):
        if isinstance(sc, dict):
            sc_name = str(sc.get("name", f"scenario_{idx}"))
            sc_explanation = str(sc.get("why", "No explanation provided."))
        else:
            sc_name = str(getattr(sc, "name", f"scenario_{idx}"))
            sc_explanation = str(getattr(sc, "explanation", getattr(sc, "why", "No explanation provided.")))
        _say(f"[{idx}] {sc_name}\nWhy this scenario exists:\n{sc_explanation}\n")

    pack_dir = project_root / "packs" / pack
    pack_dir.mkdir(parents=True, exist_ok=True)
    authoring_yaml = pack_dir / "capax.yaml"
    policy_yaml = pack_dir / "policy.yaml"
    sample_json = pack_dir / "sample_request.json"
    compiled_json = pack_dir / "compiled_policy.json"
    qa_yaml = pack_dir / "qa_scenarios.yaml"

    save_yaml(authoring_yaml, policy_to_authoring_dict(policy))
    save_yaml(policy_yaml, policy_to_dict(policy))
    sample_json.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    compile_policy(policy_yaml, compiled_json)
    save_scenarios(qa_yaml, scenarios)

    server_dir = project_root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "registry.yaml").write_text(
        "\n".join(
            [
                "routes:",
                "  - kind: admit",
                f"    pack: {pack}",
                f"    method: {protect_method}",
                f"    path: {protect_path}",
                f"    policy_yaml: packs/{pack}/policy.yaml",
                "  - kind: status",
                f"    pack: {pack}",
                f"    method: {status_method}",
                f"    path: {status_path}",
                f"    policy_yaml: packs/{pack}/policy.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )

    step += 1
    _section(f"Step {step} of 11 — Generate runtime code")
    chosen_generate_cmds: List[Tuple[str, str, Path]] = []
    selected_runtime: Tuple[str, str] = ("reference", "python")

    if _prompt_yesno(
        "Generate runtime code now before starting the server?\n\n"
        "Why we ask:\n"
        "The policy is the source of truth. Capax can scaffold runtimes so you do not need to rewrite the same guardrail by hand.",
        True,
    ):
        runtime_choices = {"1": "node", "2": "python", "3": "rust"}
        _say("Choose one or more runtimes separated by commas: [1] Node.js [2] Python [3] Rust")
        picks = [x.strip() for x in _prompt("Runtime choices:", "1,2,3").split(",") if x.strip() in runtime_choices]

        for p in picks:
            lang = runtime_choices[p]
            if lang == "node":
                framework = {"1": "express", "2": "fastify"}[_prompt_choice("Choose one Node.js framework:", {"1": "Express", "2": "Fastify"}, "1")]
            elif lang == "python":
                framework = "fastapi"
            else:
                framework = "axum"

            cmd = ["capax", "generate", "--pack", pack, "--lang", lang, "--framework", framework]
            _show_cmd(cmd)
            out_dir = generate_runtime(project_root, pack, lang, framework)
            chosen_generate_cmds.append((lang, framework, out_dir))

        _say("Generated runtime scaffolds:")
        for lang, framework, out_dir in chosen_generate_cmds:
            _say(f"• {lang}/{framework}: {out_dir}")

        if len(chosen_generate_cmds) == 1:
            lang, framework, _ = chosen_generate_cmds[0]
            selected_runtime = (lang, framework)
            _say(
                "Capax generated one runtime, so it will use that same runtime for local QA without asking again.\n"
                f"Selected runtime: {lang}/{framework}"
            )
        elif chosen_generate_cmds:
            runtime_menu = {str(i + 1): f"{lang}/{framework}" for i, (lang, framework, _) in enumerate(chosen_generate_cmds)}
            runtime_menu[str(len(chosen_generate_cmds) + 1)] = "reference/python engine"
            chosen_key = _prompt_choice(
                "Which runtime should Capax start locally for QA?\n\n"
                "Why we ask:\n"
                "Capax can run QA against a generated runtime or against the reference Python engine.\n\n"
                "What Capax will do with your answer:\n"
                "• start that exact runtime locally\n"
                "• point QA scenarios at the same runtime\n",
                runtime_menu,
                "1",
            )
            if chosen_key == str(len(chosen_generate_cmds) + 1):
                selected_runtime = ("reference", "python")
            else:
                lang, framework, _ = chosen_generate_cmds[int(chosen_key) - 1]
                selected_runtime = (lang, framework)

    step += 1
    _section(f"Step {step} of 11 — Choose a port and start the server")
    _say("Capax expects a local TCP port for the server. Recommended default: 8080.\n")
    port = _prompt_int("Server port:", 8080)
    _show_cmd(["python", "-c", f"import socket; s=socket.socket(); print(s.connect_ex(('127.0.0.1',{port})))"])
    free = _port_free(port)
    while not free:
        _say(f"Port {port} is already in use.")
        port = _prompt_int("Enter another port:", port + 1)
        _show_cmd(["python", "-c", f"import socket; s=socket.socket(); print(s.connect_ex(('127.0.0.1',{port})))"])
        free = _port_free(port)
    _say(f"Port {port} is available.")

    started_proc: Optional[subprocess.Popen] = None
    pid_file = project_root / ".capax" / f"{pack}-server.pid"

    if _prompt_yesno("Capax can start the server now in the background. Start now?", True):
        _show_cmd(["which", "nohup"])
        nohup_path = shutil.which("nohup")
        if nohup_path:
            logs_dir = project_root / ".capax"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{pack}-server.log"

            lang, framework = selected_runtime
            if lang == "reference":
                _say("Starting Capax reference server (Python runtime).")
            else:
                _say(f"Starting generated runtime: {lang}/{framework}")

            proc, _, _ = _start_runtime_background(
                project_root=project_root,
                pack=pack,
                lang=lang,
                framework=framework,
                port=port,
                log_path=log_path,
            )
            started_proc = proc
            pid_file.write_text(str(proc.pid), encoding="utf-8")
            _say(f"Server started.\nPID: {proc.pid}\nURL: http://127.0.0.1:{port}\nLogs: {log_path}")
        else:
            _say("nohup was not found. Suggested install command: sudo apt-get update && sudo apt-get install -y coreutils")

    if _prompt_yesno("Would you like Capax to run the QA scenarios now?", True):
        url = f"http://127.0.0.1:{port}"
        _show_cmd(["capax", "qa", "--pack", pack, "--url", url])
        compiled_policy = json.loads(compiled_json.read_text(encoding="utf-8"))
        scenarios = generate_scenarios(compiled_policy, sample)
        ok, errors = run_scenarios(url, compiled_policy, scenarios, progress=_say)
        report_path = project_root / ".capax" / "reports" / f"{pack}-qa-report.json"
        save_report(report_path, ok, errors, scenarios, url)
        _say(f"QA report saved: {report_path}")

        if started_proc is not None and _prompt_yesno(
            "Would you like Capax to stop the local server now and clear in-memory state for a fresh manual test?",
            True,
        ):
            _stop_local_process(started_proc, pid_file)
            _say("Local server stopped. In-memory state cleared for a fresh start.")

    _say("\nCapax setup is complete.\n")
    _say("Relevant commands:")
    if selected_runtime[0] == "reference":
        _say(f"• capax run --registry server/registry.yaml --host 127.0.0.1 --port {port}  # reference python engine")
    else:
        _say(f"• capax run --pack {pack} --lang {selected_runtime[0]} --framework {selected_runtime[1]} --host 127.0.0.1 --port {port}")
        _say(f"• capax run --registry server/registry.yaml --host 127.0.0.1 --port {port}  # reference python engine")
    _say(f"• capax qa --pack {pack} --url http://127.0.0.1:{port}")
    for lang, framework, _ in chosen_generate_cmds:
        _say(f"• capax generate --pack {pack} --lang {lang} --framework {framework}")
    _say("• capax helper")
    _say(f"• capax inspect --pack {pack}")
    _say(f"• capax rerun --pack {pack} --action compile")
    _say(f"• capax score --pack {pack} --request packs/{pack}/sample_request.json")
    _say("\nThank you for using Capax.")

