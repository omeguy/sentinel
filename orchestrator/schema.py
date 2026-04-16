# schema.py
"""
Creates orchestrator database schema.
Safe to run multiple times (idempotent).
"""

from db import DB


def create_schema(db: DB) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bots (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(128) NOT NULL,
            bot_name VARCHAR(128) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_user_bot (user_id, bot_name),
            KEY idx_user (user_id)
        ) ENGINE=InnoDB;
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_runtime (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            bot_id BIGINT UNSIGNED NOT NULL,

            status VARCHAR(32) NOT NULL DEFAULT 'creating',
            last_error TEXT NULL,

            api_port INT NULL,
            vnc_port INT NULL,
            novnc_port INT NULL,

            bot_container VARCHAR(255) NULL,
            novnc_container VARCHAR(255) NULL,

            private_network VARCHAR(255) NULL,
            mt5_volume VARCHAR(255) NULL,

            enable_vnc BOOLEAN NOT NULL DEFAULT TRUE,
            persist_volume BOOLEAN NOT NULL DEFAULT TRUE,

            updated_at TIMESTAMP NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,

            UNIQUE KEY uq_bot_runtime (bot_id),
            KEY idx_status (status),

            CONSTRAINT fk_runtime_bot
                FOREIGN KEY (bot_id)
                REFERENCES bots(id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB;
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_secrets (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            bot_id BIGINT UNSIGNED NOT NULL,

            mt5_login_enc TEXT NOT NULL,
            mt5_password_enc TEXT NOT NULL,
            mt5_server_enc TEXT NOT NULL,

            redis_namespace VARCHAR(255) NOT NULL,

            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

            UNIQUE KEY uq_bot_secrets (bot_id),

            CONSTRAINT fk_secrets_bot
                FOREIGN KEY (bot_id)
                REFERENCES bots(id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB;
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_env (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            bot_id BIGINT UNSIGNED NOT NULL,
            env_json_enc MEDIUMTEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_bot_env (bot_id),
            CONSTRAINT fk_env_bot
                FOREIGN KEY (bot_id)
                REFERENCES bots(id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB;
        """
    )

    db.execute(
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


def init_schema(db: DB, logger) -> None:
    logger.info("[SCHEMA] Initializing orchestrator schema...")
    create_schema(db)
    logger.info("[SCHEMA] Schema ready.")
