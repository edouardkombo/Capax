from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import compile_policy
from .generator import generate_runtime
from .qa import generate_scenarios, run_scenarios, save_report
from .wizard import run_wizard


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _show_cmd(cmd: List[str], extra: str = "") -> None:
    _say("Executing command:")
    _say(" ".join(cmd) + (f" {extra}" if extra else ""))


def _project_root() -> Path:
    return Path.cwd()


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pack_dir(root: Path, pack: str) -> Path:
    return root / "packs" / pack


def _load_pack_policy_for_cli(root: Path, pack: str) -> Dict[str, Any]:
    """
    For CLI execution we MUST prefer compiled_policy.json because it is the normalized,
    engine-ready source of truth. Falling back to policy.yaml causes path/field drift.
    """
    pack_dir = _pack_dir(root, pack)
    compiled_json = pack_dir / "compiled_policy.json"
    policy_yaml = pack_dir / "policy.yaml"

    if compiled_json.exists():
        return _load_json(compiled_json)

    if policy_yaml.exists():
        return _load_yaml(policy_yaml)

    raise FileNotFoundError(
        f"Neither compiled_policy.json nor policy.yaml found in {pack_dir}"
    )


def _load_pack_sample(root: Path, pack: str) -> Dict[str, Any]:
    pack_dir = _pack_dir(root, pack)
    sample_json = pack_dir / "sample_request.json"
    if not sample_json.exists():
        raise FileNotFoundError(f"Sample request not found: {sample_json}")
    return _load_json(sample_json)


def _load_pack_scenarios(root: Path, pack: str) -> List[Any]:
    """
    Always regenerate from compiled policy + sample so QA stays aligned with the same
    normalized structure the runtime actually uses.
    """
    policy = _load_pack_policy_for_cli(root, pack)
    sample = _load_pack_sample(root, pack)
    return generate_scenarios(policy, sample)


def _write_qa_report(root: Path, pack: str, ok: bool, errors: List[Dict[str, Any]], scenarios: List[Any], url: str) -> Path:
    report_path = root / ".capax" / "reports" / f"{pack}-qa-report.json"
    save_report(report_path, ok, errors, scenarios, url)
    return report_path


def _runtime_run_spec(
    root: Path,
    pack: Optional[str],
    lang: Optional[str],
    framework: Optional[str],
    registry: Optional[str],
    host: str,
    port: int,
) -> tuple[Path, Dict[str, str], List[List[str]], List[str], str]:
    env = {
        "HOST": host,
        "PORT": str(port),
        "CAPAX_ADDR": f"{host}:{port}",
        "CAPAX_ROOT": str(root),
        "CAPAX_POLICY_PATH": "compiled_policy.json",
    }

    if registry:
        cwd = root
        prep: List[List[str]] = []
        cmd = [
            sys.executable,
            "-m",
            "capax.reference_runtime",
            "--registry",
            registry,
            "--host",
            host,
            "--port",
            str(port),
        ]
        return cwd, env, prep, cmd, "reference/python"

    if not pack or not lang:
        raise ValueError("Either --registry or (--pack and --lang) is required.")

    if lang == "python":
        framework = framework or "fastapi"
        cwd = root / "gen" / pack / "python" / framework
        prep = []
        cmd = [sys.executable, "-m", "uvicorn", "app:app", "--host", host, "--port", str(port)]
        return cwd, env, prep, cmd, f"python/{framework}"

    if lang == "node":
        if framework not in {"express", "fastify"}:
            raise ValueError("Node runtime requires --framework express or --framework fastify")
        cwd = root / "gen" / pack / "node" / framework
        prep = [["npm", "install"]]
        cmd = ["npm", "start"]
        return cwd, env, prep, cmd, f"node/{framework}"

    if lang == "rust":
        framework = framework or "axum"
        cwd = root / "gen" / pack / "rust" / framework
        prep = []
        cmd = ["cargo", "run"]
        return cwd, env, prep, cmd, f"rust/{framework}"

    raise ValueError(f"Unsupported runtime language: {lang}")


def _run_background(
    cwd: Path,
    env: Dict[str, str],
    prep_cmds: List[List[str]],
    cmd: List[str],
    log_path: Path,
) -> int:
    merged_env = {**os.environ.copy(), **env}

    for prep in prep_cmds:
        _show_cmd(prep)
        subprocess.run(prep, cwd=str(cwd), env=merged_env, check=True)

    nohup = shutil.which("nohup")
    if nohup:
        full_cmd = ["nohup", *cmd]
        _show_cmd(full_cmd, f"> {log_path} 2>&1 &")
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(full_cmd, cwd=str(cwd), env=merged_env, stdout=logf, stderr=logf)
        return proc.pid

    _say("nohup was not found. Falling back to background process without nohup.")
    _show_cmd(cmd, f"> {log_path} 2>&1 &")
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=merged_env, stdout=logf, stderr=logf)
    return proc.pid


def _run_foreground(cwd: Path, env: Dict[str, str], prep_cmds: List[List[str]], cmd: List[str]) -> int:
    merged_env = {**os.environ.copy(), **env}

    for prep in prep_cmds:
        _show_cmd(prep)
        subprocess.run(prep, cwd=str(cwd), env=merged_env, check=True)

    _show_cmd(cmd)
    return subprocess.call(cmd, cwd=str(cwd), env=merged_env)


def cmd_helper(_: argparse.Namespace) -> int:
    _say("Capax helper\n")
    _say("Commands and examples:\n")

    _say("init: interactive tutorial wizard")
    _say("  capax init\n")

    _say("run: run either the reference engine or a generated runtime")
    _say("  capax run --registry server/registry.yaml --host 127.0.0.1 --port 8080")
    _say("  capax run --registry server/registry.yaml --host 127.0.0.1 --port 8080 --background")
    _say("  capax run --pack bartender --lang node --framework express --host 127.0.0.1 --port 8080")
    _say("  capax run --pack bartender --lang node --framework express --host 127.0.0.1 --port 8080 --background")
    _say("  capax run --pack bartender --lang python --framework fastapi --host 127.0.0.1 --port 8080")
    _say("  capax run --pack bartender --lang rust --framework axum --host 127.0.0.1 --port 8080\n")

    _say("qa: run QA scenarios with live progress")
    _say("  capax qa --pack bartender --url http://127.0.0.1:8080\n")

    _say("generate: generate one runtime scaffold")
    _say("  capax generate --pack bartender --lang node --framework express")
    _say("  capax generate --pack bartender --lang node --framework fastify")
    _say("  capax generate --pack bartender --lang python --framework fastapi")
    _say("  capax generate --pack bartender --lang rust --framework axum\n")

    _say("inspect: inspect current pack files")
    _say("  capax inspect --pack bartender\n")

    _say("rerun: rebuild artifacts from an existing pack")
    _say("  capax rerun --pack bartender --action compile")
    _say("  capax rerun --pack bartender --action generate --lang node --framework express")
    _say("  capax rerun --pack bartender --action qa --url http://127.0.0.1:8080\n")

    _say("score: explain request weight for a request file")
    _say("  capax score --pack bartender --request packs/bartender/sample_request.json\n")

    _say("Thank you for using Capax.")
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    run_wizard(_project_root())
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    root = _project_root()
    out_dir = generate_runtime(root, args.pack, args.lang, args.framework)
    _say(f"Generated runtime scaffold: {out_dir}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    root = _project_root()
    pack_dir = _pack_dir(root, args.pack)
    if not pack_dir.exists():
        raise FileNotFoundError(f"Pack not found: {pack_dir}")

    _say(f"Pack: {args.pack}")
    for name in ["capax.yaml", "policy.yaml", "sample_request.json", "compiled_policy.json", "qa_scenarios.yaml"]:
        path = pack_dir / name
        _say(f"• {path} {'(exists)' if path.exists() else '(missing)'}")
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    root = _project_root()
    pack_dir = _pack_dir(root, args.pack)
    policy_yaml = pack_dir / "policy.yaml"
    compiled_json = pack_dir / "compiled_policy.json"

    if args.action == "compile":
        _show_cmd(["capax", "rerun", "--pack", args.pack, "--action", "compile"])
        compile_policy(policy_yaml, compiled_json)
        scenarios = _load_pack_scenarios(root, args.pack)
        qa_yaml = pack_dir / "qa_scenarios.yaml"
        from .qa import save_scenarios
        save_scenarios(qa_yaml, scenarios)
        _say(f"Compiled policy: {compiled_json}")
        _say(f"QA scenarios: {qa_yaml}")
        return 0

    if args.action == "generate":
        if not args.lang or not args.framework:
            raise ValueError("--action generate requires --lang and --framework")
        _show_cmd(["capax", "generate", "--pack", args.pack, "--lang", args.lang, "--framework", args.framework])
        out_dir = generate_runtime(root, args.pack, args.lang, args.framework)
        _say(f"Generated runtime scaffold: {out_dir}")
        return 0

    if args.action == "qa":
        if not args.url:
            raise ValueError("--action qa requires --url")
        return cmd_qa(argparse.Namespace(pack=args.pack, url=args.url))

    raise ValueError(f"Unsupported rerun action: {args.action}")


def cmd_qa(args: argparse.Namespace) -> int:
    root = _project_root()
    policy = _load_pack_policy_for_cli(root, args.pack)
    scenarios = _load_pack_scenarios(root, args.pack)

    ok, errors = run_scenarios(args.url, policy, scenarios, progress=_say)
    report_path = _write_qa_report(root, args.pack, ok, errors, scenarios, args.url)

    _say("")
    _say(f"QA report saved: {report_path}")
    if not ok:
        _say("QA: FAIL")
        for err in errors:
            _say(str(err))
        return 1

    _say("QA: PASS")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = _project_root()
    cwd, env, prep_cmds, cmd, runtime_name = _runtime_run_spec(
        root=root,
        pack=args.pack,
        lang=args.lang,
        framework=args.framework,
        registry=args.registry,
        host=args.host,
        port=args.port,
    )

    _say(f"Selected runtime: {runtime_name}")

    if args.background:
        logs_dir = root / ".capax"
        logs_dir.mkdir(parents=True, exist_ok=True)
        pack_name = args.pack or "reference"
        log_path = logs_dir / f"{pack_name}-{args.port}.log"
        pid = _run_background(cwd, env, prep_cmds, cmd, log_path)
        _say("Server started in background.")
        _say(f"PID: {pid}")
        _say(f"URL: http://{args.host}:{args.port}")
        _say(f"Logs: {log_path}")
        return 0

    return _run_foreground(cwd, env, prep_cmds, cmd)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="capax")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("helper")
    s.set_defaults(func=cmd_helper)

    s = sub.add_parser("init")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("generate")
    s.add_argument("--pack", required=True)
    s.add_argument("--lang", required=True, choices=["node", "python", "rust"])
    s.add_argument("--framework", required=True)
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("inspect")
    s.add_argument("--pack", required=True)
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("rerun")
    s.add_argument("--pack", required=True)
    s.add_argument("--action", required=True, choices=["compile", "generate", "qa"])
    s.add_argument("--lang")
    s.add_argument("--framework")
    s.add_argument("--url")
    s.set_defaults(func=cmd_rerun)

    s = sub.add_parser("qa")
    s.add_argument("--pack", required=True)
    s.add_argument("--url", required=True)
    s.set_defaults(func=cmd_qa)

    s = sub.add_parser("run")
    s.add_argument("--registry")
    s.add_argument("--pack")
    s.add_argument("--lang", choices=["node", "python", "rust"])
    s.add_argument("--framework")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--background", action="store_true", help="run server in background and print PID/log path")
    s.set_defaults(func=cmd_run)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
