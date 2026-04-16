"""
docker_ops.py

Docker-only operations:
- ensure networks/volumes
- ensure bot infra (tracy_bridge + redis + mysql) optionally
- run/stop/remove bot + novnc containers
- fetch container logs

STRICT POLICY:
- No hardcoded defaults for settings
- If a required env var is missing -> raise ValueError
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

from dotenv import load_dotenv
import docker


# Load environment variables from .env (no-op if not present)
load_dotenv()


def _env_str_required(key: str) -> str:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        raise ValueError(f"Missing required env var: {key}")
    return val


def _env_int_required(key: str) -> int:
    val = _env_str_required(key)
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Env var {key} must be an int, got: {val!r}")


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True, slots=True)
class DockerSettings:
    # images
    bot_image: str
    novnc_image: str

    # bot infra
    infra_network: str
    redis_container: str
    mysql_container: str

    # infra autoprovision
    infra_autocreate: bool

    # redis config
    redis_image: str
    redis_port: int

    # mysql config
    mysql_image: str
    mysql_root_password: str
    mysql_database: str
    mysql_port: int

    @classmethod
    def from_env(cls) -> "DockerSettings":
        """
        STRICT: every setting must be present in env (except BOT_INFRA_AUTOCREATE).
        """
        return cls(
            bot_image=_env_str_required("BOT_IMAGE"),
            novnc_image=_env_str_required("NOVNC_IMAGE"),

            infra_network=_env_str_required("BOT_INFRA_NETWORK"),
            redis_container=_env_str_required("BOT_REDIS_CONTAINER"),
            mysql_container=_env_str_required("BOT_MYSQL_CONTAINER"),

            infra_autocreate=_env_bool("BOT_INFRA_AUTOCREATE", False),

            redis_image=_env_str_required("BOT_REDIS_IMAGE"),
            redis_port=_env_int_required("BOT_REDIS_PORT"),

            mysql_image=_env_str_required("BOT_MYSQL_IMAGE"),
            mysql_root_password=_env_str_required("BOT_MYSQL_ROOT_PASSWORD"),
            mysql_database=_env_str_required("BOT_MYSQL_DATABASE"),
            mysql_port=_env_int_required("BOT_MYSQL_PORT"),
        )


class DockerOps:
    def __init__(self, logger, settings: Optional[DockerSettings] = None):
        self.logger = logger
        self.settings = settings or DockerSettings.from_env()

        if os.getenv("ENABLE_DOCKER", "false").lower() == "true":
            self.client = docker.from_env()
        else:
            self.client = None

    # ---------------------- basics ----------------------

    def network_exists(self, name: str) -> bool:
        return any(n.name == name for n in self.client.networks.list())

    def ensure_network(self, name: str) -> None:
        if not self.network_exists(name):
            self.client.networks.create(name, driver="bridge")
            self.logger.info(f"[DOCKER] Created network: {name}")

    def volume_exists(self, name: str) -> bool:
        return any(v.name == name for v in self.client.volumes.list())

    def ensure_volume(self, name: str) -> None:
        if not self.volume_exists(name):
            self.client.volumes.create(name)
            self.logger.info(f"[DOCKER] Created volume: {name}")

    def container_exists(self, name: str) -> bool:
        try:
            self.client.containers.get(name)
            return True
        except docker.errors.NotFound:
            return False

    def container_status(self, name: str) -> Optional[str]:
        try:
            return self.client.containers.get(name).status
        except docker.errors.NotFound:
            return None

    def remove_container(self, name: str) -> bool:
        try:
            c = self.client.containers.get(name)
            c.remove(force=True)
            self.logger.info(f"[DOCKER] Removed container: {name}")
            return True
        except docker.errors.NotFound:
            return False

    def connect_container_to_network(self, container_name: str, network_name: str) -> None:
        net = self.client.networks.get(network_name)
        try:
            net.connect(container_name)
            self.logger.info(f"[DOCKER] Connected {container_name} -> {network_name}")
        except docker.errors.APIError as e:
            msg = str(e).lower()
            if "already exists" in msg or "already connected" in msg:
                return
            raise

    # ---------------------- logs ----------------------

    def get_logs(
        self,
        container_name: str,
        *,
        tail: int = 200,
        since: Optional[int] = None,
        timestamps: bool = False,
    ) -> str:
        try:
            c = self.client.containers.get(container_name)
            raw = c.logs(tail=tail, since=since, timestamps=timestamps)
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8", errors="replace")
            return str(raw)
        except docker.errors.NotFound:
            raise RuntimeError(f"Container not found: {container_name}")

    # ---------------------- bot infra ----------------------

    def ensure_bot_infra(self) -> None:
        """
        Ensures:
        - infra network exists
        - redis container exists/running
        - mysql container exists/running

        If infra_autocreate is False: fail loudly if missing.
        If True: create network and start containers if missing.
        """
        s = self.settings

        if not self.network_exists(s.infra_network):
            if not s.infra_autocreate:
                raise RuntimeError(f"Missing infra network: {s.infra_network}")
            self.ensure_network(s.infra_network)

        self._ensure_redis()
        self._ensure_mysql()

    def _ensure_redis(self) -> None:
        s = self.settings

        if self.container_exists(s.redis_container):
            c = self.client.containers.get(s.redis_container)
            if c.status != "running":
                c.start()
                self.logger.info(f"[DOCKER] Started redis: {s.redis_container}")
            return

        if not s.infra_autocreate:
            raise RuntimeError(f"Missing redis container: {s.redis_container}")

        self.logger.info(f"[DOCKER] Creating redis infra container: {s.redis_container}")

        # NOTE: we do NOT publish ports by default to avoid conflicts.
        # Bots reach redis via Docker network name.
        self.client.containers.run(
            s.redis_image,
            name=s.redis_container,
            detach=True,
            network=s.infra_network,
            restart_policy={"Name": "unless-stopped"},
            command=["redis-server", "--appendonly", "yes"],
        )

    def _ensure_mysql(self) -> None:
        s = self.settings

        if self.container_exists(s.mysql_container):
            c = self.client.containers.get(s.mysql_container)
            if c.status != "running":
                c.start()
                self.logger.info(f"[DOCKER] Started mysql: {s.mysql_container}")
            return

        if not s.infra_autocreate:
            raise RuntimeError(f"Missing mysql container: {s.mysql_container}")

        self.logger.info(f"[DOCKER] Creating mysql infra container: {s.mysql_container}")

        vol_name = f"{s.mysql_container}-data"
        self.ensure_volume(vol_name)

        # NOTE: we do NOT publish ports by default to avoid conflicts.
        self.client.containers.run(
            s.mysql_image,
            name=s.mysql_container,
            detach=True,
            network=s.infra_network,
            restart_policy={"Name": "unless-stopped"},
            environment={
                "MYSQL_ROOT_PASSWORD": s.mysql_root_password,
                "MYSQL_DATABASE": s.mysql_database,
            },
            volumes={vol_name: {"bind": "/var/lib/mysql", "mode": "rw"}},
        )

    # ---------------------- bot container ----------------------

    def run_bot_container(
        self,
        *,
        bot_container: str,
        private_network: str,
        mt5_volume: Optional[str],
        api_port: int,
        vnc_port: Optional[int],
        enable_vnc: bool,
        env: Dict[str, str],
    ) -> None:
        """
        Equivalent of:
          docker run -d --name bot-... --network ptn1-...
            -p api_port:8000 -p vnc_port:5900
            -v mt5_volume:/opt/mt5/wineprefix
            BOT_IMAGE

        Then connect container to BOT_INFRA_NETWORK so it can reach tracy-redis and tracy-mysql.
        """
        s = self.settings

        # Ensure infra exists (or fail loudly)
        self.ensure_bot_infra()

        # Ensure private network exists
        self.ensure_network(private_network)

        # Ensure mt5 volume exists (if requested)
        if mt5_volume:
            self.ensure_volume(mt5_volume)

        # If container exists, just start it + ensure infra net connect
        if self.container_exists(bot_container):
            c = self.client.containers.get(bot_container)
            if c.status != "running":
                c.start()
                self.logger.info(f"[DOCKER] Started existing bot: {bot_container}")
            self.connect_container_to_network(bot_container, s.infra_network)
            return

        ports: Dict[str, Any] = {"8000/tcp": api_port}
        if enable_vnc:
            if vnc_port is None:
                raise ValueError("enable_vnc=True but vnc_port is None")
            ports["5900/tcp"] = vnc_port

        volumes: Dict[str, Any] = {}
        if mt5_volume:
            volumes[mt5_volume] = {"bind": "/opt/mt5/wineprefix", "mode": "rw"}

        # Enforce VNC invariants ONLY if enabled
        if enable_vnc:
            env = dict(env)
            env["ENABLE_VNC"] = "true"
            env["VNC_PORT"] = "5900"

        self.logger.info(
            f"[DOCKER] Running bot {bot_container} "
            f"(api {api_port}->8000, vnc {vnc_port}->5900 if enabled) image={s.bot_image}"
        )

        self.client.containers.run(
            s.bot_image,
            name=bot_container,
            detach=True,
            network=private_network,
            environment=env,
            ports=ports,
            volumes=volumes,
            restart_policy={"Name": "unless-stopped"},
        )

        # Attach to infra network (redis/mysql)
        self.connect_container_to_network(bot_container, s.infra_network)

    def stop_bot(self, bot_container: str) -> bool:
        try:
            c = self.client.containers.get(bot_container)
            c.stop(timeout=10)
            self.logger.info(f"[DOCKER] Stopped bot: {bot_container}")
            return True
        except docker.errors.NotFound:
            return False

    # ---------------------- noVNC ----------------------

    def enable_novnc(
        self,
        *,
        novnc_container: str,
        private_network: str,
        novnc_port: int,
        vnc_host: str,
        vnc_port: int = 5900,
    ) -> None:
        s = self.settings

        self.ensure_network(private_network)

        if self.container_exists(novnc_container):
            c = self.client.containers.get(novnc_container)
            if c.status != "running":
                c.start()
                self.logger.info(f"[DOCKER] Started existing noVNC: {novnc_container}")
            return

        self.logger.info(
            f"[DOCKER] Running noVNC {novnc_container} ({novnc_port}->8080) image={s.novnc_image}"
        )

        self.client.containers.run(
            s.novnc_image,
            name=novnc_container,
            detach=True,
            network=private_network,
            environment={"VNC_HOST": vnc_host, "VNC_PORT": str(vnc_port)},
            ports={"8080/tcp": novnc_port},
            restart_policy={"Name": "unless-stopped"},
        )

    def disable_novnc(self, novnc_container: str) -> bool:
        return self.remove_container(novnc_container)
