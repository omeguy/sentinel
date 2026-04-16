# bot_service.py
"""
Business logic layer (STRICT, NO DEFAULTS):
- Creates bot rows + runtime + secrets
- Allocates ports (ports.py)
- Validates FULL bot env (every key you listed must exist)
- Saves FULL bot env to orchestrator DB (encrypted at rest)
- Builds env vars for bot containers (preserve everything, only override invariants)
- Calls DockerOps to run bot container and toggle noVNC
- Proxies engine calls to bot API
- Fetches container logs
- Uploads strategy (stored in orchestrator DB: strategies table)
"""

from __future__ import annotations

import ast
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, List

import requests
from cryptography.fernet import Fernet, InvalidToken

from db import DB
from ports import allocate_ports_for_bot
from docker_ops import DockerOps

import mysql.connector



def _env_str_required(key: str) -> str:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        raise ValueError(f"Missing required env var: {key}")
    return v


def _env_str(key: str, default: str) -> str:
    v = os.getenv(key)
    return default if v is None or v.strip() == "" else v


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    return int(v)


def _sanitize(s: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in s)
    return out[:80]


def docker_names(user_id: str, bot_name: str) -> Dict[str, str]:
    u = _sanitize(user_id)
    b = _sanitize(bot_name)
    key = f"{u}-{b}"
    return {
        "key": key,
        "net": f"ptn1-{key}",
        "bot": f"bot-{key}",
        "novnc": f"novnc-{key}",
        "vol": f"mt5-{key}",
    }


@dataclass(slots=True)
class BotCreateRequest:
    user_id: str
    bot_name: str
    env: Dict[str, str]
    enable_vnc: bool = True
    persist_volume: bool = True
    enable_novnc: bool = False


class BotService:
    # EXACTLY your env list (every last env key you pasted)
    REQUIRED_PRESENT_KEYS: List[str] = [
        "APP_MODE",
        "LOG_LEVEL",
        "MARKETS",
        "BOT_NAME",

        "ENGINE_AUTOSTART",
        "ENGINE_AUTOSTART_LOCK_TTL",
        "ENGINE_STOP_TIMEOUT",
        "ENGINE_LOG_TO_FILE",
        "ENGINE_LOG_DIR",
        "ENGINE_LOG_FILE",
        "ENGINE_LOG_MAX_BYTES",
        "ENGINE_LOG_ROTATE_KEEP",

        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "REDIS_PASSWORD",
        "REDIS_PREFIX",
        "REDIS_MAX_CONNECTIONS",
        "REDIS_POS_TTL",
        "REDIS_HB_TTL",
        "REDIS_LOCK_TTL",

        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
        "MT5_TIMEFRAME",

        "LOT",
        "DEVIATION",
        "PIP_RANGE",
        "FROM_DATA",
        "TO_DATA",
        "MAGIC",
        "TP_PIPS",
        "ATR_PERIOD",
        "ATR_SL_MULTIPLIER",
        "MAX_DIST_ATR_MULTIPLIER",
        "TRAIL_ATR_MULTIPLIER",

        "DISCORD_WEBHOOK_URL",
        "MESSENGER_USERNAME",
        "NOTIFY_CHANNEL",

        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASS",
        "DB_NAME",
        "DB_POOL_NAME",
        "DB_POOL_SIZE",
        "DB_CONNECT_TIMEOUT",

        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASS",
        "EMAIL_FROM",
        "EMAIL_TO",

        "BOT_NODE_MAP",
    ]

    REQUIRED_NONEMPTY_KEYS: List[str] = [
        "APP_MODE",
        "LOG_LEVEL",
        "MARKETS",
        "BOT_NAME",

        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "REDIS_PREFIX",

        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
        "MT5_TIMEFRAME",

        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASS",
        "DB_NAME",

        "NOTIFY_CHANNEL",
    ]

    def __init__(self, db: DB, docker_ops: DockerOps, logger: Any):
        self.db = db
        self.docker = docker_ops
        self.logger = logger

        key = _env_str_required("SECRETS_FERNET_KEY")
        try:
            self.fernet = Fernet(key.encode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Invalid SECRETS_FERNET_KEY: {e}")

        self.bot_api_host = _env_str("BOT_API_HOST", "127.0.0.1")
        self.bot_api_timeout = _env_int("BOT_API_TIMEOUT", 10)

    # ---------------------- crypto helpers ----------------------

    def _enc(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def _dec(self, value_enc: str) -> str:
        try:
            return self.fernet.decrypt(value_enc.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise RuntimeError("Secret decryption failed (wrong SECRETS_FERNET_KEY?)")

    # ---------------------- schema addons ----------------------

    def ensure_env_table(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_env (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                bot_id BIGINT UNSIGNED NOT NULL,
                env_json_enc MEDIUMTEXT NOT NULL,
                env_sha256 VARCHAR(64) NULL,
                updated_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_bot_env (bot_id),
                CONSTRAINT fk_bot_env_bot
                    FOREIGN KEY (bot_id)
                    REFERENCES bots(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB;
            """
        )

    def ensure_bot_registry_tables(self) -> dict:
        """
        Ensure bot_registry database + required tables exist.
        Safe to run multiple times.
        """

        s = self.docker.settings

        host = getattr(s, "mysql_container", None)
        port = getattr(s, "mysql_port", None)
        root_pw = getattr(s, "mysql_root_password", None)
        dbname = getattr(s, "mysql_database", None)

        if not host or not port or not root_pw or not dbname:
            raise RuntimeError(
                "Missing bot-mysql settings. Need BOT_MYSQL_CONTAINER/BOT_MYSQL_PORT/"
                "BOT_MYSQL_ROOT_PASSWORD/BOT_MYSQL_DATABASE set."
            )

        created = []
        already = []

        conn = None
        cur = None

        try:
            # ---------------------------
            # STEP 1: CONNECT WITHOUT DB
            # ---------------------------
            conn = mysql.connector.connect(
                host=str(host),
                port=int(port),
                user="root",
                password=str(root_pw),
                autocommit=True,   # ✅ important
            )
            cur = conn.cursor()

            # ---------------------------
            # STEP 2: CREATE DATABASE
            # ---------------------------
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}`")

            # check if DB exists (for response clarity)
            cur.execute("SHOW DATABASES LIKE %s", (dbname,))
            if cur.fetchone():
                already.append("database")

            cur.close()
            conn.close()

            # ---------------------------
            # STEP 3: RECONNECT WITH DB
            # ---------------------------
            conn = mysql.connector.connect(
                host=str(host),
                port=int(port),
                user="root",
                password=str(root_pw),
                database=str(dbname),
                autocommit=True,
            )
            cur = conn.cursor()

            # ---------------------------
            # STEP 4: CREATE TABLES
            # ---------------------------

            # strategies table
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) UNIQUE,
                    class_name VARCHAR(255) NOT NULL,
                    python_code LONGTEXT,
                    approved BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NULL,
                    approved_at TIMESTAMP NULL
                ) ENGINE=InnoDB;
                """
            )

            cur.execute("SHOW TABLES LIKE 'strategies'")
            if cur.fetchone():
                already.append("strategies")

            return {
                "ok": True,
                "database": str(dbname),
                "created": created,
                "already_present": already,
            }

        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
    # ---------------------- DB helpers ----------------------

    def _get_bot_id(self, user_id: str, bot_name: str) -> Optional[int]:
        row = self.db.fetchone(
            "SELECT id FROM bots WHERE user_id=%s AND bot_name=%s",
            (user_id, bot_name),
        )
        return int(row[0]) if row else None

    def _get_runtime(self, bot_id: int) -> dict:
        row = self.db.fetchone(
            """
            SELECT status, last_error, api_port, vnc_port, novnc_port,
                   bot_container, novnc_container, private_network, mt5_volume,
                   enable_vnc, persist_volume
            FROM bot_runtime
            WHERE bot_id=%s
            """,
            (bot_id,),
            as_dict=True,
        )
        if not row:
            raise RuntimeError("bot_runtime missing for bot_id")
        return row

    def _get_secrets(self, bot_id: int) -> dict:
        row = self.db.fetchone(
            """
            SELECT mt5_login_enc, mt5_password_enc, mt5_server_enc, redis_namespace
            FROM bot_secrets
            WHERE bot_id=%s
            """,
            (bot_id,),
            as_dict=True,
        )
        if not row:
            raise RuntimeError("bot_secrets missing for bot_id")
        return row

    # ---------------------- strict env validation ----------------------

    def _require_present(self, env: Dict[str, str], keys: List[str]) -> None:
        missing = [k for k in keys if k not in env]
        if missing:
            raise ValueError(f"Missing required bot env keys (must be present): {missing}")

    def _require_nonempty(self, env: Dict[str, str], keys: List[str]) -> None:
        bad = []
        for k in keys:
            if k not in env:
                bad.append(k)
                continue
            v = env.get(k)
            if v is None or str(v).strip() == "":
                bad.append(k)
        if bad:
            raise ValueError(f"Missing/empty required bot env keys: {bad}")

    def validate_bot_env(self, env: Dict[str, str], expected_bot_name: str) -> None:
        self._require_present(env, self.REQUIRED_PRESENT_KEYS)
        self._require_nonempty(env, self.REQUIRED_NONEMPTY_KEYS)

        if str(env.get("BOT_NAME", "")).strip() != expected_bot_name:
            raise ValueError("BOT_NAME in env must match bot_name in request")

        for k, v in env.items():
            if v is None:
                raise ValueError(f"Bot env key {k} has None value (not allowed)")

    # ---------------------- env persistence ----------------------

    def save_full_env(self, bot_id: int, env: Dict[str, str]) -> None:
        self.ensure_env_table()

        normalized: Dict[str, str] = {}
        for k, v in env.items():
            if v is None:
                raise ValueError(f"Bot env key {k} is None (not allowed)")
            normalized[k] = v if isinstance(v, str) else str(v)

        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        payload_enc = self._enc(payload)

        self.db.execute(
            """
            INSERT INTO bot_env (bot_id, env_json_enc)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                env_json_enc=VALUES(env_json_enc),
                updated_at=CURRENT_TIMESTAMP
            """,
            (bot_id, payload_enc),
        )

    # ---------------------- bot record upsert ----------------------

    def _derive_redis_namespace(self, req: BotCreateRequest) -> str:
        prefix = str(req.env["REDIS_PREFIX"]).strip()
        return f"{prefix}:{req.user_id}:{req.bot_name}"

    def _upsert_bot_records(self, req: BotCreateRequest) -> int:
        bot_id = self._get_bot_id(req.user_id, req.bot_name)
        if bot_id is None:
            bot_id = self.db.insert(
                "INSERT INTO bots(user_id, bot_name) VALUES(%s, %s)",
                (req.user_id, req.bot_name),
            )

            names = docker_names(req.user_id, req.bot_name)
            self.db.execute(
                """
                INSERT INTO bot_runtime(
                    bot_id, status, bot_container, novnc_container, private_network, mt5_volume,
                    enable_vnc, persist_volume
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    bot_id,
                    "creating",
                    names["bot"],
                    names["novnc"],
                    names["net"],
                    names["vol"],
                    bool(req.enable_vnc),
                    bool(req.persist_volume),
                ),
            )

            mt5_login = str(req.env["MT5_LOGIN"])
            mt5_password = str(req.env["MT5_PASSWORD"])
            mt5_server = str(req.env["MT5_SERVER"])
            redis_namespace = self._derive_redis_namespace(req)

            self.db.execute(
                """
                INSERT INTO bot_secrets(
                    bot_id, mt5_login_enc, mt5_password_enc, mt5_server_enc, redis_namespace
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    bot_id,
                    self._enc(mt5_login),
                    self._enc(mt5_password),
                    self._enc(mt5_server),
                    redis_namespace,
                ),
            )

            return bot_id

        self.db.execute(
            """
            UPDATE bot_runtime
            SET enable_vnc=%s, persist_volume=%s
            WHERE bot_id=%s
            """,
            (bool(req.enable_vnc), bool(req.persist_volume), bot_id),
        )
        return bot_id

    # ---------------------- env building ----------------------

    def _build_bot_env_for_container(self, req_env: Dict[str, str], secrets: dict, req: BotCreateRequest) -> Dict[str, str]:
        env = dict(req_env)

        env["BOT_NAME"] = req.bot_name
        env["REDIS_NAMESPACE"] = secrets["redis_namespace"]
        env["MT5_LOGIN"] = self._dec(secrets["mt5_login_enc"])
        env["MT5_PASSWORD"] = self._dec(secrets["mt5_password_enc"])
        env["MT5_SERVER"] = self._dec(secrets["mt5_server_enc"])

        if req.enable_vnc:
            env["ENABLE_VNC"] = "true"
            env["VNC_PORT"] = "5900"
        else:
            env.pop("ENABLE_VNC", None)
            env.pop("VNC_PORT", None)

        for k, v in env.items():
            if v is None:
                raise ValueError(f"Bot env key {k} has None value (not allowed)")
        for k, v in list(env.items()):
            if not isinstance(v, str):
                env[k] = str(v)
        return env

    # ---------------------- bot api / readiness ----------------------

    def _bot_api_base(self, api_port: int) -> str:
        return f"http://{self.bot_api_host}:{api_port}"

    def wait_ready(self, api_port: int, timeout_s: int = 60) -> bool:
        url = f"{self._bot_api_base(api_port)}/ready"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=2)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(1.5)
        return False

    # ---------------------- public operations ----------------------

    def create_bot(self, req: BotCreateRequest) -> dict:
        self.validate_bot_env(req.env, expected_bot_name=req.bot_name)
        bot_id = self._upsert_bot_records(req)
        self.save_full_env(bot_id, req.env)
        secrets = self._get_secrets(bot_id)

        ports = allocate_ports_for_bot(
            self.db,
            req.user_id,
            req.bot_name,
            enable_vnc=bool(req.enable_vnc),
            want_novnc=bool(req.enable_novnc),
        )

        names = docker_names(req.user_id, req.bot_name)
        container_env = self._build_bot_env_for_container(req.env, secrets, req)

        self.docker.run_bot_container(
            bot_container=names["bot"],
            private_network=names["net"],
            mt5_volume=(names["vol"] if req.persist_volume else None),
            api_port=int(ports["api_port"]),
            vnc_port=(int(ports["vnc_port"]) if ports.get("vnc_port") else None),
            enable_vnc=bool(req.enable_vnc),
            env=container_env,
        )

        novnc_enabled = False
        if req.enable_novnc:
            if ports.get("novnc_port") is None:
                raise RuntimeError("novnc_port missing after allocation")
            self.docker.enable_novnc(
                novnc_container=names["novnc"],
                private_network=names["net"],
                novnc_port=int(ports["novnc_port"]),
                vnc_host=names["bot"],
            )
            novnc_enabled = True

        ready = self.wait_ready(int(ports["api_port"]), timeout_s=60)

        self.db.execute(
            "UPDATE bot_runtime SET status=%s, last_error=%s WHERE bot_id=%s",
            ("running" if ready else "degraded", None if ready else "ready timeout", bot_id),
        )

        return {
            "user_id": req.user_id,
            "bot_name": req.bot_name,
            "bot_id": bot_id,
            "status": "running" if ready else "degraded",
            "ports": ports,
            "docker": {
                "bot_container": names["bot"],
                "novnc_container": names["novnc"],
                "private_network": names["net"],
                "mt5_volume": names["vol"],
            },
            "urls": {
                "api_url": self._bot_api_base(int(ports["api_port"])),
                "novnc_url": (f"http://{self.bot_api_host}:{int(ports['novnc_port'])}/vnc.html" if novnc_enabled else None),
            },
        }

    def get_bot_status(self, user_id: str, bot_name: str) -> dict:
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")

        rt = self._get_runtime(bot_id)
        names = docker_names(user_id, bot_name)

        bot_status = self.docker.container_status(names["bot"])
        novnc_status = self.docker.container_status(names["novnc"])

        ready = False
        engine_status: Any = "unknown"
        api_port = rt.get("api_port")
        if api_port:
            try:
                r = requests.get(f"{self._bot_api_base(int(api_port))}/ready", timeout=2)
                ready = (r.status_code == 200)
            except Exception:
                ready = False

            try:
                r = requests.get(f"{self._bot_api_base(int(api_port))}/engine/status", timeout=2)
                if r.status_code == 200:
                    engine_status = r.json()
            except Exception:
                pass

        return {
            "user_id": user_id,
            "bot_name": bot_name,
            "status": rt["status"],
            "docker": {
                "bot_container": names["bot"],
                "bot_container_status": bot_status,
                "novnc_container": names["novnc"],
                "novnc_container_status": novnc_status,
            },
            "ports": {
                "api_port": rt.get("api_port"),
                "vnc_port": rt.get("vnc_port"),
                "novnc_port": rt.get("novnc_port"),
            },
            "health": {
                "ready": ready,
                "engine_status": engine_status,
            },
        }

    def enable_novnc(self, user_id: str, bot_name: str) -> dict:
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")

        rt = self._get_runtime(bot_id)
        if not rt.get("api_port"):
            raise RuntimeError("Bot has no api_port (not created?)")

        ports = allocate_ports_for_bot(
            self.db,
            user_id,
            bot_name,
            enable_vnc=bool(rt["enable_vnc"]),
            want_novnc=True,
        )

        names = docker_names(user_id, bot_name)
        self.docker.enable_novnc(
            novnc_container=names["novnc"],
            private_network=names["net"],
            novnc_port=int(ports["novnc_port"]),
            vnc_host=names["bot"],
        )

        return {
            "ok": True,
            "novnc": {
                "enabled": True,
                "novnc_port": int(ports["novnc_port"]),
                "novnc_url": f"http://{self.bot_api_host}:{int(ports['novnc_port'])}/vnc.html",
                "container": names["novnc"],
            },
        }

    def disable_novnc(self, user_id: str, bot_name: str) -> dict:
        names = docker_names(user_id, bot_name)
        removed = self.docker.disable_novnc(names["novnc"])
        return {"ok": True, "removed": bool(removed)}

    def engine_start(self, user_id: str, bot_name: str) -> dict:
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")
        rt = self._get_runtime(bot_id)
        api_port = rt.get("api_port")
        if not api_port:
            raise RuntimeError("api_port not allocated")

        r = requests.post(f"{self._bot_api_base(int(api_port))}/engine/start", timeout=self.bot_api_timeout)
        return {"ok": r.ok, "status_code": r.status_code, "result": self._safe_json(r)}

    def engine_stop(self, user_id: str, bot_name: str) -> dict:
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")
        rt = self._get_runtime(bot_id)
        api_port = rt.get("api_port")
        if not api_port:
            raise RuntimeError("api_port not allocated")

        r = requests.post(f"{self._bot_api_base(int(api_port))}/engine/stop", timeout=self.bot_api_timeout)
        return {"ok": r.ok, "status_code": r.status_code, "result": self._safe_json(r)}

    def engine_status(self, user_id: str, bot_name: str) -> dict:
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")
        rt = self._get_runtime(bot_id)
        api_port = rt.get("api_port")
        if not api_port:
            raise RuntimeError("api_port not allocated")

        r = requests.get(f"{self._bot_api_base(int(api_port))}/engine/status", timeout=self.bot_api_timeout)
        return {"ok": r.ok, "status_code": r.status_code, "result": self._safe_json(r)}

    def _safe_json(self, r: requests.Response):
        try:
            return r.json()
        except Exception:
            return {"text": r.text}

    def get_container_logs(self, user_id: str, bot_name: str, *, which: str = "bot", tail: int = 200) -> dict:
        names = docker_names(user_id, bot_name)
        if which not in ("bot", "novnc"):
            raise ValueError("which must be 'bot' or 'novnc'")
        container = names["bot"] if which == "bot" else names["novnc"]
        logs = self.docker.get_logs(container, tail=tail, timestamps=False)
        return {"container": container, "tail": tail, "logs": logs}

    # ---------------------- strategy upload (matches old script behavior) ----------------------

    def upload_strategy(
        self,
        user_id: str,
        bot_name: str,
        *,
        name: str,
        class_name: str,
        python_code: str,
        approved: bool,
        mode: str = "upload",
        validate_syntax: bool = True,
    ) -> dict:
        m = (mode or "").strip().lower()
        if m not in ("upload", "update"):
            raise ValueError("mode must be 'upload' or 'update'")

        if not (name or "").strip():
            raise ValueError("name is required")
        if not (class_name or "").strip():
            raise ValueError("class_name is required")
        if not (python_code or "").strip():
            raise ValueError("python_code is required")

        # keep your current policy: strategy upload is scoped to a bot existing
        bot_id = self._get_bot_id(user_id, bot_name)
        if bot_id is None:
            raise RuntimeError("Bot not found")

        if validate_syntax:
            try:
                ast.parse(python_code)
            except SyntaxError as e:
                raise ValueError(f"Python syntax error at line {e.lineno}: {e.msg}")

        now = datetime.utcnow()
        approved_int = int(bool(approved))
        approved_at = now if approved else None

        if m == "update":
            row = self.db.fetchone(
                "SELECT id FROM strategies WHERE name=%s LIMIT 1",
                (name,),
                as_dict=False,
            )
            if not row:
                raise RuntimeError(f"Strategy '{name}' does NOT exist. Use mode='upload' first.")
            strategy_id = int(row[0])

            self.db.execute(
                """
                UPDATE strategies
                SET class_name=%s,
                    python_code=%s,
                    approved=%s,
                    updated_at=%s,
                    approved_at=%s
                WHERE name=%s
                """,
                (class_name, python_code, approved_int, now, approved_at, name),
            )

            return {
                "ok": True,
                "action": "update",
                "strategy_id": strategy_id,
                "name": name,
                "class_name": class_name,
                "approved": bool(approved),
            }

        # upload mode = upsert
        self.db.execute(
            """
            INSERT INTO strategies (name, class_name, python_code, approved, created_at, updated_at, approved_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                class_name = VALUES(class_name),
                python_code = VALUES(python_code),
                approved = VALUES(approved),
                updated_at = VALUES(updated_at),
                approved_at = VALUES(approved_at);
            """,
            (name, class_name, python_code, approved_int, now, now, approved_at),
        )

        row = self.db.fetchone(
            "SELECT id FROM strategies WHERE name=%s LIMIT 1",
            (name,),
            as_dict=False,
        )
        strategy_id = int(row[0]) if row else None

        return {
            "ok": True,
            "action": "upload",
            "strategy_id": strategy_id,
            "name": name,
            "class_name": class_name,
            "approved": bool(approved),
        }
