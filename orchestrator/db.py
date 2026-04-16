# db.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple, Sequence

import os
from dotenv import load_dotenv

import mysql.connector
from mysql.connector import pooling, Error

load_dotenv()


def _env_str(key: str, default: str | None = None) -> str:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        if default is None:
            raise ValueError(f"Missing required env var: {key}")
        return default
    return val


def _env_one_of(keys: Sequence[str]) -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            return v
    raise ValueError(f"Missing required env var: one of {list(keys)}")


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Env var {key} must be an int, got: {val!r}")


@dataclass(slots=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    name: str
    pool_name: str
    pool_size: int
    connect_timeout: int

    @classmethod
    def from_env(cls) -> "DBConfig":
        return cls(
            host=_env_str("DB_HOST"),
            port=_env_int("DB_PORT", 3306),
            user=_env_str("DB_USER"),
            password=_env_one_of(["DB_PASSWORD", "DB_PASS"]),
            name=_env_str("DB_NAME"),
            pool_name=_env_str("DB_POOL_NAME", "orch_pool"),
            pool_size=_env_int("DB_POOL_SIZE", 10),
            connect_timeout=_env_int("DB_CONNECT_TIMEOUT", 10),
        )


@dataclass(slots=True)
class DB:
    config: DBConfig
    logger: Any
    _pool: Optional[pooling.MySQLConnectionPool] = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._init_pool()

    def _init_pool(self) -> None:
        try:
            temp_cnx = mysql.connector.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                connection_timeout=self.config.connect_timeout,
                use_pure=True,
            )
            cur = temp_cnx.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.config.name}`")

            temp_cnx.commit()

            cur.close()
            temp_cnx.close()

            self.logger.info(f"[DB] Database ready: {self.config.name}")

            self._pool = pooling.MySQLConnectionPool(
                pool_name=self.config.pool_name,
                pool_size=self.config.pool_size,
                pool_reset_session=True,
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.name,
                connection_timeout=self.config.connect_timeout,
                autocommit=False,
                use_pure=True,
            )

            self.logger.info(f"[DB] Pool initialized: {self.config.pool_name} size={self.config.pool_size}")

        except Exception as e:
            self.logger.error(f"[DB] Pool init FAILED → {e}")
            raise

    def _get_conn(self):
        if not self._pool:
            raise RuntimeError("DB pool not initialized")
        try:
            conn = self._pool.get_connection()
            conn.ping(reconnect=True, attempts=2, delay=1)
            return conn
        except Exception as e:
            self.logger.error(f"[DB] Connection error → {e}")
            raise

    def execute(self, sql: str, params: Optional[Tuple] = None) -> int:
        conn = None
        cur = None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
        except Error as e:
            if conn:
                conn.rollback()
            self.logger.error(f"[DB] Execute error: {e} | SQL={sql}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    def insert(self, sql: str, params: Optional[Tuple] = None) -> int:
        conn = None
        cur = None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            return int(cur.lastrowid)
        except Error as e:
            if conn:
                conn.rollback()
            self.logger.error(f"[DB] Insert error: {e} | SQL={sql}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    def executemany(self, sql: str, params_list: Sequence[Tuple]) -> int:
        conn = None
        cur = None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.executemany(sql, params_list)
            conn.commit()
            return cur.rowcount
        except Error as e:
            if conn:
                conn.rollback()
            self.logger.error(f"[DB] ExecuteMany error: {e} | SQL={sql}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    def fetchone(self, sql: str, params=None, as_dict: bool = False):
        conn = None
        cur = None
        try:
            conn = self._get_conn()
            cur = conn.cursor(dictionary=as_dict)
            cur.execute(sql, params)
            return cur.fetchone()
        except Error as e:
            self.logger.error(f"[DB] FetchOne error: {e} | SQL={sql}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    def fetchall(self, sql: str, params=None, as_dict: bool = False):
        conn = None
        cur = None
        try:
            conn = self._get_conn()
            cur = conn.cursor(dictionary=as_dict)
            cur.execute(sql, params)
            return cur.fetchall()
        except Error as e:
            self.logger.error(f"[DB] FetchAll error: {e} | SQL={sql}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()


def make_db(logger: Any) -> DB:
    cfg = DBConfig.from_env()
    return DB(cfg, logger)
