from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import CapacityGate
from .config import load_policy_yaml


def load_registry(registry_path: Path) -> Dict[str, Any]:
    return yaml.safe_load(registry_path.read_text(encoding="utf-8"))


def build_app(registry_path: Path) -> FastAPI:
    reg = load_registry(registry_path)
    app = FastAPI(title="Capax")
    gates: Dict[str, CapacityGate] = {}

    def gate_for(pack_name: str, policy_yaml: Path) -> CapacityGate:
        if pack_name in gates:
            return gates[pack_name]
        policy = load_policy_yaml(policy_yaml)
        gates[pack_name] = CapacityGate(policy)
        return gates[pack_name]

    for r in reg.get("routes", []):
        method = (r.get("method") or "POST").upper()
        path = r.get("path") or "/order"
        pack = r.get("pack") or "default"
        policy_yaml = Path(r.get("policy_yaml") or f"packs/{pack}/policy.yaml")
        kind = r.get("kind") or "admit"

        if kind == "status":
            async def _status(pack_name: str = pack, policy_path: Path = policy_yaml):
                g = gate_for(pack_name, policy_path)
                payload = await g.status()
                return JSONResponse(status_code=200, content=payload)
            app.add_api_route(path, _status, methods=[method])
            continue

        async def _admit(req: Request, pack_name: str = pack, policy_path: Path = policy_yaml, m: str = method, p: str = path):
            g = gate_for(pack_name, policy_path)
            try:
                body = await req.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                return JSONResponse(status_code=400, content={"result": "rejected", "reason": "body_must_be_object"})
            status_code, payload = await g.admit(m, p, body)
            return JSONResponse(status_code=status_code, content=payload)

        app.add_api_route(path, _admit, methods=[method])

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
