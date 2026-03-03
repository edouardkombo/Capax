from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from .config import compile_policy, load_policy_yaml
from .server import build_app
from .wizard import run_wizard
from .qa import generate_scenarios, run_scenarios
from .generator import generate_runtime


def _repo_root(p: str | None) -> Path:
    return Path(p).resolve() if p else Path.cwd().resolve()


def cmd_wizard(args: argparse.Namespace) -> int:
    run_wizard(_repo_root(args.root))
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    root = _repo_root(args.root)
    policy_yaml = root / "packs" / args.pack / "policy.yaml"
    out_json = root / "packs" / args.pack / "compiled_policy.json"
    compile_policy(policy_yaml, out_json)
    print(f"Compiled policy: {out_json}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = _repo_root(args.root)
    reg = root / args.registry
    app = build_app(reg)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    root = _repo_root(args.root)
    policy = load_policy_yaml(root / "packs" / args.pack / "policy.yaml")
    sample = json.loads((root / "packs" / args.pack / "sample_request.json").read_text(encoding="utf-8"))
    scenarios = generate_scenarios(policy, sample)
    ok, errors = run_scenarios(args.http, policy, scenarios)
    if ok:
        print("QA: PASS")
        return 0
    print("QA: FAIL")
    for e in errors:
        print("-", e)
    return 2


def cmd_generate(args: argparse.Namespace) -> int:
    root = _repo_root(args.root)
    out = generate_runtime(root, args.pack, args.lang)
    print(f"Generated: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="capax", description="Capax — query admission control (weighted concurrency)")
    p.add_argument("--root", default=None, help="Project root (defaults to current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wizard", help="Interactive setup")
    w.set_defaults(func=cmd_wizard)

    c = sub.add_parser("compile", help="Compile policy.yaml into compiled_policy.json")
    c.add_argument("--pack", required=True)
    c.set_defaults(func=cmd_compile)

    r = sub.add_parser("run", help="Run HTTP server (python)")
    r.add_argument("--registry", default="server/registry.yaml")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--log-level", default="info")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("test", help="Run dynamic HTTP QA derived from config (prints computed cost)")
    t.add_argument("--pack", required=True)
    t.add_argument("--http", required=True)
    t.set_defaults(func=cmd_test)

    g = sub.add_parser("generate", help="Generate a runtime server in another language")
    g.add_argument("--pack", required=True)
    g.add_argument("--lang", required=True, choices=["python", "node", "rust"])
    g.set_defaults(func=cmd_generate)

    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
