from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

Lang = Literal["python", "node", "rust"]
Framework = Literal["express", "fastify", "fastapi", "axum"]

EXPRESS_SERVER = r'''
const express = require("express");
const fs = require("fs");
const crypto = require("crypto");
const app = express();
app.use(express.json({limit: "1mb"}));

function stableJson(x) { return JSON.stringify(x, Object.keys(x).sort()); }
function sha256Hex(s) { return crypto.createHash("sha256").update(s).digest("hex"); }
function nowMs() { return Date.now(); }
function splitTokens(raw, seps){ let toks=[raw]; for(const sep of seps||["|"," OR "," or "]){ const next=[]; for(const t of toks){ if(typeof t === "string" && t.includes(sep)) next.push(...t.split(sep).map(s=>s.trim()).filter(Boolean)); else next.push(String(t).trim()); } toks=next; } return [...new Set(toks.filter(Boolean))]; }
function makeEngine(policy){
  const states = new Map();
  const field = policy.cost_expression.field;
  const tokenCosts = policy.cost_expression.token_costs || {};
  const tokenCostsCI = Object.fromEntries(Object.entries(tokenCosts).map(([k,v])=>[String(k).toLowerCase(), v]));
  function bucket(body){ const iso = (((policy||{}).capacity||{}).isolation)||{}; if(!iso.enabled || !iso.field) return "shared"; return `${iso.field}=${body?.[iso.field]}`; }
  function stateFor(k){ if(!states.has(k)) states.set(k, {inFlightCost:0, inFlight:new Map(), served:[], idempo:new Map()}); return states.get(k); }
  function tokenCost(tok){ if(Object.prototype.hasOwnProperty.call(tokenCosts, tok)) return [Number(tokenCosts[tok]), null]; const k=String(tok).toLowerCase(); if(policy.cost_expression.case_insensitive && Object.prototype.hasOwnProperty.call(tokenCostsCI,k)) return [Number(tokenCostsCI[k]), null]; const u=policy.cost_expression.unknown||{action:"default_cost",default_cost:1}; if(u.action==="reject_400") return [null, `unknown_token:${tok}`]; return [Number(u.default_cost||1), null]; }
  function computeCost(body){ if(!field) return [1,null]; if(!(field in body) || body[field]===null || body[field]===""){ const m=policy.cost_expression.missing_field||{action:"default_cost",default_cost:1}; if(m.action==="reject_400") return [null, `missing_field:${field}`]; return [Number(m.default_cost||1), null]; }
    const raw=body[field]; if(typeof raw !== 'string') return tokenCost(String(raw)); const toks=splitTokens(String(raw).trim(), policy.cost_expression.separators||["|"," OR "," or "]); const costs=[]; for(const t of toks){ const [c,e]=tokenCost(t); if(e) return [null,e]; costs.push(Number(c)); } if(!costs.length) return [1,null]; if(costs.length===1) return [costs[0], null]; return [(policy.cost_expression.combine_mode==="bundle_sum") ? costs.reduce((a,b)=>a+b,0) : Math.max(...costs), null]; }
  function status(){ let total=0, served=0; const byBucket={}; const servedOrders=[]; for (const [k,st] of states){ total += st.inFlightCost; served += st.served.length; byBucket[k]={inFlightCost:st.inFlightCost,inFlightCount:st.inFlight.size,servedCount:st.served.length}; for (const rec of st.served.slice(-50)) servedOrders.push(rec); } return { inFlightCost: total, servedCount: served, servedOrders, byBucket }; }
  function admit(method, path, body){ const key=bucket(body); const st=stateFor(key); const fp=sha256Hex(`${method} ${path}\n${stableJson(body)}`); const idem=policy.idempotency||{enabled:false,ttl_seconds:600}; if(idem.enabled && st.idempo.has(fp)){ const entry=st.idempo.get(fp); if(nowMs() < entry.exp){ return [entry.status, {...entry.payload, idempotent_replay:true}]; } st.idempo.delete(fp); }
    const [cost, err]=computeCost(body); if(err){ const payload={result:"rejected", reason:err, cost:null, bucket:key}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.bad_request),payload}); return [Number(policy.http.codes.bad_request), payload]; }
    const cap=Number(policy.capacity.max_allowed_concurrent_capacity); if(st.inFlightCost + Number(cost) > cap){ const payload={result:"rejected", reason:"at_capacity", cost:Number(cost), bucket:key, in_flight_cost:st.inFlightCost, capacity:cap}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.at_capacity),payload}); return [Number(policy.http.codes.at_capacity), payload]; }
    const orderId=fp.slice(0,12); const rec={orderId,bucket:key,cost:Number(cost),admittedMs:nowMs(),state:"in_flight"}; st.inFlightCost += Number(cost); st.inFlight.set(orderId,rec); setTimeout(()=>{ const x=st.inFlight.get(orderId); if(!x) return; st.inFlight.delete(orderId); st.inFlightCost = Math.max(0, st.inFlightCost - x.cost); x.state="served"; x.servedMs=nowMs(); st.served.push(x); }, Math.max(0, Number(policy.capacity.duration_seconds)*1000)); const payload={result:"accepted", orderId, cost:Number(cost), bucket:key, in_flight_cost:st.inFlightCost, capacity:cap}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.accepted),payload}); return [Number(policy.http.codes.accepted), payload]; }
  return {status, admit};
}

const policyPath = process.env.CAPAX_POLICY_PATH || "compiled_policy.json";
const policy = JSON.parse(fs.readFileSync(policyPath, "utf8"));
const engine = makeEngine(policy);
const host = process.env.HOST || "127.0.0.1"; const port = Number(process.env.PORT || 8080);
const protect = policy.http.protect; const observe = policy.http.observe;
app[observe.method.toLowerCase()](observe.path, (_req, res) => res.status(200).json(engine.status()));
app[protect.method.toLowerCase()](protect.path, (req, res) => {
  const body = req.body;
  if(typeof body !== 'object' || body===null || Array.isArray(body)) return res.status(Number(policy.http.codes.bad_request)).json({result:"rejected", reason:"body_must_be_object"});
  const [status, payload] = engine.admit(protect.method, protect.path, body);
  return res.status(status).json(payload);
});
app.listen(port, host, () => {
  console.log(`Capax Express on http://${host}:${port}`);
  console.log(`protect -> ${protect.method} ${protect.path}`);
  console.log(`observe -> ${observe.method} ${observe.path}`);
});
'''

FASTIFY_SERVER = r'''
const Fastify = require("fastify");
const fs = require("fs");
const crypto = require("crypto");
const app = Fastify({ logger: false });

function stableJson(x) { return JSON.stringify(x, Object.keys(x).sort()); }
function sha256Hex(s) { return crypto.createHash("sha256").update(s).digest("hex"); }
function nowMs() { return Date.now(); }
function splitTokens(raw, seps){ let toks=[raw]; for(const sep of seps||["|"," OR "," or "]){ const next=[]; for(const t of toks){ if(typeof t === "string" && t.includes(sep)) next.push(...t.split(sep).map(s=>s.trim()).filter(Boolean)); else next.push(String(t).trim()); } toks=next; } return [...new Set(toks.filter(Boolean))]; }
function makeEngine(policy){
  const states = new Map();
  const field = policy.cost_expression.field;
  const tokenCosts = policy.cost_expression.token_costs || {};
  const tokenCostsCI = Object.fromEntries(Object.entries(tokenCosts).map(([k,v])=>[String(k).toLowerCase(), v]));
  function bucket(body){ const iso = (((policy||{}).capacity||{}).isolation)||{}; if(!iso.enabled || !iso.field) return "shared"; return `${iso.field}=${body?.[iso.field]}`; }
  function stateFor(k){ if(!states.has(k)) states.set(k, {inFlightCost:0, inFlight:new Map(), served:[], idempo:new Map()}); return states.get(k); }
  function tokenCost(tok){ if(Object.prototype.hasOwnProperty.call(tokenCosts, tok)) return [Number(tokenCosts[tok]), null]; const k=String(tok).toLowerCase(); if(policy.cost_expression.case_insensitive && Object.prototype.hasOwnProperty.call(tokenCostsCI,k)) return [Number(tokenCostsCI[k]), null]; const u=policy.cost_expression.unknown||{action:"default_cost",default_cost:1}; if(u.action==="reject_400") return [null, `unknown_token:${tok}`]; return [Number(u.default_cost||1), null]; }
  function computeCost(body){ if(!field) return [1,null]; if(!(field in body) || body[field]===null || body[field]===""){ const m=policy.cost_expression.missing_field||{action:"default_cost",default_cost:1}; if(m.action==="reject_400") return [null, `missing_field:${field}`]; return [Number(m.default_cost||1), null]; }
    const raw=body[field]; if(typeof raw !== 'string') return tokenCost(String(raw)); const toks=splitTokens(String(raw).trim(), policy.cost_expression.separators||["|"," OR "," or "]); const costs=[]; for(const t of toks){ const [c,e]=tokenCost(t); if(e) return [null,e]; costs.push(Number(c)); } if(!costs.length) return [1,null]; if(costs.length===1) return [costs[0], null]; return [(policy.cost_expression.combine_mode==="bundle_sum") ? costs.reduce((a,b)=>a+b,0) : Math.max(...costs), null]; }
  function status(){ let total=0, served=0; const byBucket={}; const servedOrders=[]; for (const [k,st] of states){ total += st.inFlightCost; served += st.served.length; byBucket[k]={inFlightCost:st.inFlightCost,inFlightCount:st.inFlight.size,servedCount:st.served.length}; for (const rec of st.served.slice(-50)) servedOrders.push(rec); } return { inFlightCost: total, servedCount: served, servedOrders, byBucket }; }
  function admit(method, path, body){ const key=bucket(body); const st=stateFor(key); const fp=sha256Hex(`${method} ${path}\n${stableJson(body)}`); const idem=policy.idempotency||{enabled:false,ttl_seconds:600}; if(idem.enabled && st.idempo.has(fp)){ const entry=st.idempo.get(fp); if(nowMs() < entry.exp){ return [entry.status, {...entry.payload, idempotent_replay:true}]; } st.idempo.delete(fp); }
    const [cost, err]=computeCost(body); if(err){ const payload={result:"rejected", reason:err, cost:null, bucket:key}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.bad_request),payload}); return [Number(policy.http.codes.bad_request), payload]; }
    const cap=Number(policy.capacity.max_allowed_concurrent_capacity); if(st.inFlightCost + Number(cost) > cap){ const payload={result:"rejected", reason:"at_capacity", cost:Number(cost), bucket:key, in_flight_cost:st.inFlightCost, capacity:cap}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.at_capacity),payload}); return [Number(policy.http.codes.at_capacity), payload]; }
    const orderId=fp.slice(0,12); const rec={orderId,bucket:key,cost:Number(cost),admittedMs:nowMs(),state:"in_flight"}; st.inFlightCost += Number(cost); st.inFlight.set(orderId,rec); setTimeout(()=>{ const x=st.inFlight.get(orderId); if(!x) return; st.inFlight.delete(orderId); st.inFlightCost = Math.max(0, st.inFlightCost - x.cost); x.state="served"; x.servedMs=nowMs(); st.served.push(x); }, Math.max(0, Number(policy.capacity.duration_seconds)*1000)); const payload={result:"accepted", orderId, cost:Number(cost), bucket:key, in_flight_cost:st.inFlightCost, capacity:cap}; if(idem.enabled) st.idempo.set(fp,{exp:nowMs()+idem.ttl_seconds*1000,status:Number(policy.http.codes.accepted),payload}); return [Number(policy.http.codes.accepted), payload]; }
  return {status, admit};
}

const policyPath = process.env.CAPAX_POLICY_PATH || "compiled_policy.json";
const policy = JSON.parse(fs.readFileSync(policyPath, "utf8"));
const engine = makeEngine(policy);
const host = process.env.HOST || "127.0.0.1"; const port = Number(process.env.PORT || 8080);
const protect = policy.http.protect; const observe = policy.http.observe;
app.route({ method: observe.method, url: observe.path, handler: async () => engine.status() });
app.route({ method: protect.method, url: protect.path, handler: async (req, reply) => { const body = req.body; if(typeof body !== 'object' || body===null || Array.isArray(body)) return reply.code(Number(policy.http.codes.bad_request)).send({result:"rejected", reason:"body_must_be_object"}); const [status, payload] = engine.admit(protect.method, protect.path, body); return reply.code(status).send(payload); }});
app.listen({ port, host }).then(() => {
  console.log(`Capax Fastify on http://${host}:${port}`);
  console.log(`protect -> ${protect.method} ${protect.path}`);
  console.log(`observe -> ${observe.method} ${observe.path}`);
});
'''

FASTAPI_APP = r'''
import os
from pathlib import Path
from capax.server import build_app

root = Path(os.environ.get("CAPAX_ROOT", ".")).resolve()
app = build_app(root / "server" / "registry.yaml")
'''

AXUM_MAIN = r'''
use std::{collections::HashMap, fs, sync::Arc, time::{Duration, SystemTime, UNIX_EPOCH}};
use axum::{extract::State, http::{Method, StatusCode}, response::IntoResponse, routing::on, Json, Router};
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;

#[derive(Clone)]
struct IdempoEntry { exp_ms: i64, status: u16, payload: Value }
#[derive(Clone)]
struct Record { order_id: String, bucket: String, cost: i64, admitted_ms: i64, served_ms: Option<i64>, state: String }
#[derive(Default)]
struct BucketState { in_flight_cost: i64, in_flight: HashMap<String, Record>, served: Vec<Record>, idempo: HashMap<String, IdempoEntry> }
#[derive(Clone)]
struct AppState { policy: Arc<Value>, states: Arc<Mutex<HashMap<String, BucketState>>> }

fn now_ms() -> i64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64 }
fn split_tokens(raw: &str, seps: &[String]) -> Vec<String> { let mut toks = vec![raw.to_string()]; for sep in seps { let mut next = Vec::new(); for t in toks { if t.contains(sep) { for part in t.split(sep) { let s = part.trim(); if !s.is_empty() { next.push(s.to_string()); } } } else { let s=t.trim(); if !s.is_empty() { next.push(s.to_string()); } } } toks = next; } toks.sort(); toks.dedup(); toks }
fn bucket(policy: &Value, body: &Map<String, Value>) -> String { let iso = &policy["capacity"]["isolation"]; if !iso["enabled"].as_bool().unwrap_or(false) { return "shared".into(); } let field = iso["field"].as_str().unwrap_or(""); format!("{}={}", field, body.get(field).cloned().unwrap_or(Value::Null)) }
fn token_cost(policy: &Value, tok: &str) -> Result<i64, String> { let costs = policy["cost_expression"]["token_costs"].as_object().cloned().unwrap_or_default(); if let Some(v) = costs.get(tok) { return Ok(v.as_i64().unwrap_or(1)); } let ci = policy["cost_expression"]["case_insensitive"].as_bool().unwrap_or(true); if ci { let lc = tok.to_lowercase(); for (k,v) in costs { if k.to_lowercase() == lc { return Ok(v.as_i64().unwrap_or(1)); } } } let u = &policy["cost_expression"]["unknown"]; if u["action"].as_str().unwrap_or("default_cost") == "reject_400" { return Err(format!("unknown_token:{}", tok)); } Ok(u["default_cost"].as_i64().unwrap_or(1)) }
fn compute_cost(policy: &Value, body: &Map<String, Value>) -> Result<i64, String> { let field = policy["cost_expression"]["field"].as_str().unwrap_or(""); if field.is_empty() { return Ok(1); } let missing = &policy["cost_expression"]["missing_field"]; let Some(raw) = body.get(field) else { return if missing["action"].as_str().unwrap_or("default_cost") == "reject_400" { Err(format!("missing_field:{}", field)) } else { Ok(missing["default_cost"].as_i64().unwrap_or(1)) }; }; if raw.is_null() { return if missing["action"].as_str().unwrap_or("default_cost") == "reject_400" { Err(format!("missing_field:{}", field)) } else { Ok(missing["default_cost"].as_i64().unwrap_or(1)) }; } if let Some(s) = raw.as_str() { if s.trim().is_empty() { return if missing["action"].as_str().unwrap_or("default_cost") == "reject_400" { Err(format!("missing_field:{}", field)) } else { Ok(missing["default_cost"].as_i64().unwrap_or(1)) }; } let seps = policy["cost_expression"]["separators"].as_array().cloned().unwrap_or_default().into_iter().filter_map(|v| v.as_str().map(|x| x.to_string())).collect::<Vec<_>>(); let toks = split_tokens(s.trim(), &seps); let mut costs = Vec::new(); for t in toks { costs.push(token_cost(policy, &t)?); } if costs.len() <= 1 { return Ok(*costs.first().unwrap_or(&1)); } if policy["cost_expression"]["combine_mode"].as_str().unwrap_or("choice_max") == "bundle_sum" { return Ok(costs.iter().sum()); } return Ok(*costs.iter().max().unwrap()); } token_cost(policy, &raw.to_string()) }
fn fingerprint(method: &str, path: &str, body: &Value) -> String { use sha2::{Digest, Sha256}; let mut hasher = Sha256::new(); hasher.update(method.as_bytes()); hasher.update(b" "); hasher.update(path.as_bytes()); hasher.update(b"\n"); hasher.update(serde_json::to_vec(body).unwrap()); format!("{:x}", hasher.finalize()) }

async fn protect(State(state): State<AppState>, Json(body): Json<Value>) -> impl IntoResponse { let bad = state.policy["http"]["codes"]["bad_request"].as_u64().unwrap_or(400) as u16; let at_cap = state.policy["http"]["codes"]["at_capacity"].as_u64().unwrap_or(429) as u16; let accepted = state.policy["http"]["codes"]["accepted"].as_u64().unwrap_or(200) as u16; let cap = state.policy["capacity"]["max_allowed_concurrent_capacity"].as_i64().unwrap_or(1); let hold_ms = (state.policy["capacity"]["duration_seconds"].as_f64().unwrap_or(1.0) * 1000.0) as u64; let idem_enabled = state.policy["idempotency"]["enabled"].as_bool().unwrap_or(false); let idem_ttl_ms = state.policy["idempotency"]["ttl_seconds"].as_i64().unwrap_or(600) * 1000; let body_map = match body.as_object() { Some(m) => m.clone(), None => return (StatusCode::from_u16(bad).unwrap(), Json(json!({"result":"rejected","reason":"body_must_be_object"}))) }; let protect = &state.policy["http"]["protect"]; let method = protect["method"].as_str().unwrap_or("POST"); let path = protect["path"].as_str().unwrap_or("/order"); let fp = fingerprint(method, path, &body); let bucket_key = bucket(&state.policy, &body_map); let mut states = state.states.lock().await; let st = states.entry(bucket_key.clone()).or_default(); if idem_enabled { if let Some(entry) = st.idempo.get(&fp) { if now_ms() < entry.exp_ms { let mut payload = entry.payload.clone(); if let Some(obj)=payload.as_object_mut() { obj.insert("idempotent_replay".into(), Value::Bool(true)); } return (StatusCode::from_u16(entry.status).unwrap(), Json(payload)); } } } let cost = match compute_cost(&state.policy, &body_map) { Ok(c) => c, Err(e) => { let payload = json!({"result":"rejected","reason":e,"cost":Value::Null,"bucket":bucket_key}); if idem_enabled { st.idempo.insert(fp.clone(), IdempoEntry{exp_ms: now_ms()+idem_ttl_ms, status: bad, payload: payload.clone()}); } return (StatusCode::from_u16(bad).unwrap(), Json(payload)); } }; if st.in_flight_cost + cost > cap { let payload = json!({"result":"rejected","reason":"at_capacity","cost":cost,"bucket":bucket_key,"in_flight_cost":st.in_flight_cost,"capacity":cap}); if idem_enabled { st.idempo.insert(fp.clone(), IdempoEntry{exp_ms: now_ms()+idem_ttl_ms, status: at_cap, payload: payload.clone()}); } return (StatusCode::from_u16(at_cap).unwrap(), Json(payload)); } let order_id = fp[..12.min(fp.len())].to_string(); let rec = Record{ order_id: order_id.clone(), bucket: bucket_key.clone(), cost, admitted_ms: now_ms(), served_ms: None, state: "in_flight".into() }; st.in_flight_cost += cost; st.in_flight.insert(order_id.clone(), rec); let payload = json!({"result":"accepted","orderId":order_id,"cost":cost,"bucket":bucket_key,"in_flight_cost":st.in_flight_cost,"capacity":cap}); if idem_enabled { st.idempo.insert(fp.clone(), IdempoEntry{exp_ms: now_ms()+idem_ttl_ms, status: accepted, payload: payload.clone()}); } drop(states); let states2 = state.states.clone(); let bucket2 = bucket_key.clone(); let order2 = order_id.clone(); tokio::spawn(async move { tokio::time::sleep(Duration::from_millis(hold_ms)).await; let mut states = states2.lock().await; if let Some(st) = states.get_mut(&bucket2) { if let Some(mut rec) = st.in_flight.remove(&order2) { st.in_flight_cost = std::cmp::max(0, st.in_flight_cost - rec.cost); rec.served_ms = Some(now_ms()); rec.state = "served".into(); st.served.push(rec); } } }); (StatusCode::from_u16(accepted).unwrap(), Json(payload)) }
async fn observe(State(state): State<AppState>) -> impl IntoResponse { let states = state.states.lock().await; let mut total = 0_i64; let mut served_count = 0_usize; let mut by_bucket = Map::new(); let mut served_orders = Vec::new(); for (k, st) in states.iter() { total += st.in_flight_cost; served_count += st.served.len(); by_bucket.insert(k.clone(), json!({"inFlightCost": st.in_flight_cost, "inFlightCount": st.in_flight.len(), "servedCount": st.served.len()})); for rec in st.served.iter().rev().take(50) { served_orders.push(json!({"orderId": rec.order_id, "bucket": rec.bucket, "cost": rec.cost, "state": rec.state, "servedMs": rec.served_ms, "admittedMs": rec.admitted_ms})); } } Json(json!({"inFlightCost": total, "servedCount": served_count, "servedOrders": served_orders, "byBucket": by_bucket})) }

#[tokio::main]
async fn main() { let policy_path = std::env::var("CAPAX_POLICY_PATH").unwrap_or_else(|_| "compiled_policy.json".into()); let policy: Value = serde_json::from_str(&fs::read_to_string(policy_path).unwrap()).unwrap(); let state = AppState { policy: Arc::new(policy.clone()), states: Arc::new(Mutex::new(HashMap::new())) }; let protect_path = policy["http"]["protect"]["path"].as_str().unwrap_or("/order").to_string(); let observe_path = policy["http"]["observe"]["path"].as_str().unwrap_or("/status").to_string(); let protect_method = policy["http"]["protect"]["method"].as_str().unwrap_or("POST").parse::<Method>().unwrap_or(Method::POST); let observe_method = policy["http"]["observe"]["method"].as_str().unwrap_or("GET").parse::<Method>().unwrap_or(Method::GET); let app = Router::new().route(&protect_path, on(protect_method, protect)).route(&observe_path, on(observe_method, observe)).with_state(state); let addr = std::env::var("CAPAX_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".into()); let listener = tokio::net::TcpListener::bind(addr).await.unwrap(); axum::serve(listener, app).await.unwrap(); }
'''

CARGO_TOML = '''[package]
name = "capax-runtime"
version = "0.1.0"
edition = "2021"

[dependencies]
axum = "0.7"
tokio = { version = "1", features = ["full"] }
serde_json = "1"
sha2 = "0.10"
'''


def default_framework_for(lang: Lang) -> Framework:
    return {"node": "express", "python": "fastapi", "rust": "axum"}[lang]


def framework_choices_for(lang: Lang) -> list[str]:
    if lang == "node":
        return ["express", "fastify"]
    if lang == "python":
        return ["fastapi"]
    return ["axum"]


def generate_runtime(project_root: Path, pack: str, lang: Lang, framework: Optional[str] = None) -> Path:
    fw = framework or default_framework_for(lang)
    if fw not in framework_choices_for(lang):
        raise ValueError(f"Unsupported framework '{fw}' for lang '{lang}'")
    out_dir = project_root / "gen" / pack / lang / fw
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_dir = project_root / "packs" / pack
    compiled = pack_dir / "compiled_policy.json"
    if not compiled.exists():
        raise FileNotFoundError(f"Missing {compiled}. Run: capax compile --pack {pack}")

    if lang == "node":
        server_code = EXPRESS_SERVER if fw == "express" else FASTIFY_SERVER
        package = {
            "name": f"capax-{pack}-{fw}",
            "version": "0.0.1",
            "private": True,
            "type": "commonjs",
            "scripts": {"start": "node server.js"},
            "dependencies": {fw: "latest"},
        }
        (out_dir / "server.js").write_text(server_code, encoding="utf-8")
        (out_dir / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
    elif lang == "python":
        (out_dir / "app.py").write_text(FASTAPI_APP, encoding="utf-8")
        (out_dir / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    else:
        src = out_dir / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "main.rs").write_text(AXUM_MAIN, encoding="utf-8")
        (out_dir / "Cargo.toml").write_text(CARGO_TOML, encoding="utf-8")

    (out_dir / "compiled_policy.json").write_text(compiled.read_text(encoding="utf-8"), encoding="utf-8")
    return out_dir
