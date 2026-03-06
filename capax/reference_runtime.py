from __future__ import annotations

import argparse
import inspect
from pathlib import Path
from typing import Any, Dict

import uvicorn
import yaml

from . import server as server_mod


def _load_registry(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _try_build_app(registry_path: Path):
    registry_dict = _load_registry(registry_path)

    # Try explicit builder names first
    for name in ["build_app_from_registry", "create_app_from_registry", "app_from_registry"]:
        fn = getattr(server_mod, name, None)
        if callable(fn):
            try:
                return fn(registry_path)
            except TypeError:
                try:
                    return fn(str(registry_path))
                except TypeError:
                    return fn(registry_dict)

    # Then generic builders
    for name in ["build_app", "create_app", "make_app", "app"]:
        fn = getattr(server_mod, name, None)
        if callable(fn):
            try:
                sig = inspect.signature(fn)
                if len(sig.parameters) == 0:
                    return fn()
            except Exception:
                pass

            # Try common variants
            for candidate in (registry_path, str(registry_path), registry_dict):
                try:
                    return fn(candidate)
                except TypeError:
                    continue

    raise RuntimeError(
        "Could not find a usable reference app builder in capax.server. "
        "Expected one of: build_app_from_registry, create_app_from_registry, "
        "app_from_registry, build_app, create_app, make_app."
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m capax.reference_runtime")
    p.add_argument("--registry", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    app = _try_build_app(Path(args.registry))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
