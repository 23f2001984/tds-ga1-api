"""TDS GA1 — one FastAPI app for Q6 (/effective-config), Q14 (/mcp) and Q15 (/answer)."""
import hashlib
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

EMAIL = "23f2001984@ds.study.iitm.ac.in"
ROOT = Path(__file__).resolve().parent  # config files sit next to this file

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

KNOWN = ("/effective-config", "/mcp", "/answer")


@app.middleware("http")
async def fix_vercel_path(request: Request, call_next):
    """Vercel can hand us '/api/index...' or '/api/...' instead of the URL the
    user typed. Strip those prefixes so our routes match either way."""
    path = request.scope["path"]
    # vercel.json rewrites /<anything> -> /api/index?__path=<anything>,
    # so the URL the user typed arrives as a query parameter.
    original = request.query_params.get("__path")
    if original is not None:
        path = "/" + original.lstrip("/")
    for prefix in ("/api/index.py", "/api/index", "/api"):
        if path == prefix or path.startswith(prefix + "/"):
            path = path[len(prefix):] or "/"
            break
    request.scope["path"] = path
    request.scope["raw_path"] = path.encode()
    return await call_next(request)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    # Debug helper: shows which path the server actually received.
    return JSONResponse(
        {"detail": "Not Found", "path_seen": request.scope.get("path"),
         "root_path": request.scope.get("root_path"), "known_routes": KNOWN},
        status_code=404,
    )


@app.get("/")
def home():
    return {"status": "ok", "routes": ["/effective-config", "/mcp", "/answer"]}


# ---------------------------------------------------------------- Q6
DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}
ALIASES = {"num_workers": "workers"}


def norm_key(k: str) -> str:
    """APP_LOG_LEVEL -> log_level, NUM_WORKERS -> workers."""
    k = k.strip().lower()
    if k.startswith("app_"):
        k = k[4:]
    return ALIASES.get(k, k)


def read_dotenv(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[norm_key(k)] = v.strip().strip('"').strip("'")
    return out


def coerce(key: str, value):
    if key in ("port", "workers"):
        return int(str(value).strip())
    if key == "debug":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return str(value)


@app.get("/effective-config")
def effective_config(request: Request):
    env_name = os.environ.get("APP_ENV", "development")
    cfg = dict(DEFAULTS)                                        # 1. defaults

    yaml_path = ROOT / f"config.{env_name}.yaml"                # 2. YAML
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        cfg.update({norm_key(k): v for k, v in data.items()})

    cfg.update(read_dotenv(ROOT / "app.env"))                   # 3. .env file

    for k, v in os.environ.items():                             # 4. OS env (APP_*)
        if k.upper().startswith("APP_") and k.upper() != "APP_ENV":
            cfg[norm_key(k)] = v

    for item in request.query_params.getlist("set"):            # 5. CLI overrides
        if "=" in item:
            k, v = item.split("=", 1)
            cfg[norm_key(k)] = v

    result = {k: coerce(k, v) for k, v in cfg.items()}
    result["api_key"] = "****"                                  # never expose the secret
    return result


# ---------------------------------------------------------------- Q14
TOOL = {
    "name": "solve_challenge",
    "description": "Returns sha256(challenge:email)[:16] using the X-Exam-Challenge header.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


def rpc_result(req_id, result):
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


@app.get("/mcp")
def mcp_get():
    # No server-initiated stream; clients fall back to plain POST.
    return Response(status_code=405)


@app.post("/mcp")
async def mcp(request: Request):
    msg = await request.json()
    if isinstance(msg, list):  # batch: answer each request in turn
        out = []
        for m in msg:
            r = handle_rpc(m, request)
            if r is not None:
                out.append(r)
        return JSONResponse(out) if out else Response(status_code=202)
    r = handle_rpc(msg, request)
    return JSONResponse(r) if r is not None else Response(status_code=202)


def handle_rpc(msg: dict, request: Request):
    method, req_id = msg.get("method"), msg.get("id")
    if req_id is None:  # notification (e.g. notifications/initialized): no reply
        return None
    if method == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tds-ga1-mcp", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        if name != "solve_challenge":
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        challenge = request.headers.get("x-exam-challenge", "")
        digest = hashlib.sha256(f"{challenge}:{EMAIL.strip().lower()}".encode()).hexdigest()[:16]
        result = {"content": [{"type": "text", "text": digest}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


# ---------------------------------------------------------------- Q15
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402


@app.post("/answer")
async def answer(request: Request):
    body = await request.json()
    question = str(body.get("question", ""))
    try:
        result, spec, source = ledger.answer(question)
    except Exception as e:  # never crash the grader's request
        return {"answer": None, "error": str(e)}
    out = {"answer": result}
    if body.get("debug"):
        out["parsed"] = {k: (str(v) if k == "filters" else v) for k, v in spec.items()}
        out["data_source"] = source
    return out
