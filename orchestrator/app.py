# app.py
"""
FastAPI HTTP layer for the Tracy Orchestrator.
This exposes endpoints your website will call.

STRICT POLICY:
- create bot requires FULL env payload (all required keys)
- orchestrator does not default bot env vars
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, Literal

from fastapi import FastAPI, HTTPException, Query, Path, Request
from pydantic import BaseModel, Field

from fastapi.openapi.utils import get_openapi

from db import make_db
from schema import init_schema
from docker_ops import DockerOps
from bot_service import BotService, BotCreateRequest

from fastapi.staticfiles import StaticFiles # Needed to serve CSS/JS
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse # Added File/HTMLResponse
# ---------------------- startup validation ----------------------

def _require_fernet_key() -> None:
    key = os.getenv("SECRETS_FERNET_KEY", "").strip()
    if not key or key.upper() == "REPLACE_ME":
        raise RuntimeError(
            "SECRETS_FERNET_KEY missing or REPLACE_ME. Generate with:\n"
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def _orch_api_key() -> str:
    key = os.getenv("ORCH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ORCH_API_KEY missing. Set ORCH_API_KEY in .env")
    return key


_require_fernet_key()
_API_KEY = _orch_api_key()


# ---------------------- logging ----------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")


# ---------------------- FastAPI + Swagger API key auth ----------------------

API_KEY_HEADER_NAME = "x-api-key"

app = FastAPI(
    title="Tracy Orchestrator",
    version="1.0.0",
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows your UI on port 80 to talk to this API
    allow_credentials=True,
    allow_methods=["*"],  # Allows GET, POST, OPTIONS, etc.
    allow_headers=["*"],  # Allows x-api-key and Content-Type
)
# -------------------------------------------------------------


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )

    schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": API_KEY_HEADER_NAME,
    }

    # Apply globally so Swagger sends it for all endpoints
    schema["security"] = [{"ApiKeyAuth": []}]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ---------------------- API key middleware ----------------------

@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    # allow health/docs without auth
    if request.url.path in ("/health", "/docs", "/openapi.json"):
        return await call_next(request)

    #got = request.headers.get(API_KEY_HEADER_NAME, "").strip()
    #if got != _API_KEY:
    #    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


# ---------------------- request models ----------------------

class CreateBotBody(BaseModel):
    bot_name: str = Field(..., min_length=1, max_length=128)

    env: Dict[str, str] = Field(
        ...,
        description="Full bot env payload (ALL required keys).",
        example={
            "APP_MODE": "dev",
            "LOG_LEVEL": "DEBUG",
            "MARKETS": "forex",
            "BOT_NAME": "demo-bot",

            "ENGINE_AUTOSTART": "0",
            "ENGINE_AUTOSTART_LOCK_TTL": "30",
            "ENGINE_STOP_TIMEOUT": "5",
            "ENGINE_LOG_TO_FILE": "1",
            "ENGINE_LOG_DIR": "/app/logs",
            "ENGINE_LOG_FILE": "engine.log",
            "ENGINE_LOG_MAX_BYTES": "1000000",
            "ENGINE_LOG_ROTATE_KEEP": "3",

            "REDIS_HOST": "tracy-redis",
            "REDIS_PORT": "6379",
            "REDIS_DB": "0",
            "REDIS_PASSWORD": "",
            "REDIS_PREFIX": "tracy",
            "REDIS_MAX_CONNECTIONS": "10",
            "REDIS_POS_TTL": "0",
            "REDIS_HB_TTL": "30",
            "REDIS_LOCK_TTL": "10",

            "MT5_LOGIN": "123456",
            "MT5_PASSWORD": "password",
            "MT5_SERVER": "Demo-MT5",
            "MT5_TIMEFRAME": "TIMEFRAME_M15",

            "LOT": "0.01",
            "DEVIATION": "10",
            "PIP_RANGE": "10",
            "FROM_DATA": "1",
            "TO_DATA": "16",
            "MAGIC": "777",
            "TP_PIPS": "20",
            "ATR_PERIOD": "14",
            "ATR_SL_MULTIPLIER": "0.1",
            "MAX_DIST_ATR_MULTIPLIER": "0.4",
            "TRAIL_ATR_MULTIPLIER": "0.2",

            "DISCORD_WEBHOOK_URL": "",
            "MESSENGER_USERNAME": "demo",
            "NOTIFY_CHANNEL": "console",

            "DB_HOST": "tracy-mysql",
            "DB_PORT": "3306",
            "DB_USER": "root",
            "DB_PASS": "root",
            "DB_NAME": "bot_registry",
            "DB_POOL_NAME": "bot_pool",
            "DB_POOL_SIZE": "5",
            "DB_CONNECT_TIMEOUT": "5",

            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "demo@gmail.com",
            "SMTP_PASS": "pass",
            "EMAIL_FROM": "demo@gmail.com",
            "EMAIL_TO": "demo@gmail.com",

            "BOT_NODE_MAP": "DemoStrategy:forex:EURUSD"
        }
    )

    enable_vnc: bool = True
    persist_volume: bool = True
    enable_novnc: bool = False


class UploadStrategyBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    class_name: str = Field(..., min_length=1, max_length=255)
    python_code: str = Field(..., min_length=1)
    approved: bool = False
    mode: Literal["upload", "update"] = "upload"
    validate_syntax: bool = True


# ---------------------- lifecycle: build singletons ----------------------

db = make_db(logger)
init_schema(db, logger)

docker_ops = DockerOps(logger)
svc = BotService(db, docker_ops, logger)


# ---------------------- helpers ----------------------

def _as_str_dict(env: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in env.items():
        if v is None:
            raise ValueError(f"Env key {k} has None value (not allowed)")
        out[str(k)] = v if isinstance(v, str) else str(v)
    return out


def _http_error_from_exc(e: Exception) -> HTTPException:
    msg = str(e)

    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=msg)

    low = msg.lower()
    if "not found" in low:
        return HTTPException(status_code=404, detail=msg)

    return HTTPException(status_code=500, detail=msg)


# ---------------------- routes ----------------------

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/v1/infra/bot-db/ensure")
def ensure_bot_db_tables():
    """
    Run FIRST when setting up a new machine/infra.
    Ensures required tables exist in bot_registry (tracy-mysql).
    """
    try:
        return svc.ensure_bot_registry_tables()
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots")
def create_bot(
    user_id: str = Path(..., min_length=1, max_length=128),
    body: CreateBotBody = ...,
):
    try:
        env = _as_str_dict(body.env)
        req = BotCreateRequest(
            user_id=user_id,
            bot_name=body.bot_name,
            env=env,
            enable_vnc=body.enable_vnc,
            persist_volume=body.persist_volume,
            enable_novnc=body.enable_novnc,
        )
        return svc.create_bot(req)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.get("/v1/users/{user_id}/bots/{bot_name}")
def get_bot_status(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.get_bot_status(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots/{bot_name}/engine/start")
def engine_start(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.engine_start(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots/{bot_name}/engine/stop")
def engine_stop(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.engine_stop(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.get("/v1/users/{user_id}/bots/{bot_name}/engine/status")
def engine_status(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.engine_status(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots/{bot_name}/ui/novnc/enable")
def enable_novnc(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.enable_novnc(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots/{bot_name}/ui/novnc/disable")
def disable_novnc(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
):
    try:
        return svc.disable_novnc(user_id, bot_name)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.get("/v1/users/{user_id}/bots/{bot_name}/logs")
def get_logs(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
    which: Literal["bot", "novnc"] = Query("bot"),
    tail: int = Query(200, ge=1, le=5000),
):
    try:
        return svc.get_container_logs(user_id, bot_name, which=which, tail=tail)
    except Exception as e:
        raise _http_error_from_exc(e)


@app.post("/v1/users/{user_id}/bots/{bot_name}/strategy")
def upload_strategy(
    user_id: str = Path(..., min_length=1, max_length=128),
    bot_name: str = Path(..., min_length=1, max_length=128),
    body: UploadStrategyBody = ...,
):
    try:
        return svc.upload_strategy(
            user_id=user_id,
            bot_name=bot_name,
            name=body.name,
            class_name=body.class_name,
            python_code=body.python_code,
            approved=body.approved,
            mode=body.mode,
            validate_syntax=body.validate_syntax,
        )
    except Exception as e:
        raise _http_error_from_exc(e)


# --- SERVE THE UI ---

# 1. This tells FastAPI to look into the "sentinal-ui" folder for your assets
# It assumes your HTML/CSS/JS are in a folder named 'sentinal-ui'
if os.path.exists("sentinal-ui"):
    app.mount("/static", StaticFiles(directory="sentinal-ui"), name="static")

# 2. This serves your index.html when you visit http://40.124.81.146:9000/
@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_path = os.path.join("sentinal-ui", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("UI folder 'sentinal-ui' not found. Check your directory structure.")