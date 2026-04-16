# upload_strategy.py
# Fill the SETTINGS below, then run:
#   python upload_strategy.py

import os
import ast
import mysql.connector
from datetime import datetime


# =========================================================
# ✅ SETTINGS: FILL THESE BEFORE RUNNING
# =========================================================

MODE = "upload"  # "upload" or "update"

STRATEGY_NAME = "LondonBreakUserBot"  # strategies.name
CLASS_NAME    = "LondonBreakUserBot"  # strategies.class_name (IMPORTANT!)
FILE_PATH     = r"C:\Users\tegao\Desktop\Tracy_v1.0\tracy\bots\londonBreakUserBot.py"

APPROVED = True  # True/False

# ---- DB connection (your docker mapping is 3307 -> 3306)
DB_HOST = "127.0.0.1"
DB_PORT = 3307
DB_USER = "root"
DB_PASS = "root"
DB_NAME = "bot_registry"

# =========================================================


def read_file(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"[ERROR] File not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def validate_python_syntax(code: str, file_path: str) -> None:
    try:
        ast.parse(code)
        print("[OK] Python syntax validated.")
    except SyntaxError as e:
        raise SyntaxError(f"[SYNTAX ERROR] {file_path} → Line {e.lineno}: {e.msg}")


def ensure_database_exists() -> None:
    """
    ✅ Create DB if it doesn't exist.
    We must connect WITHOUT specifying database=DB_NAME first,
    because DB might not exist yet.
    """
    conn = mysql.connector.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASS,
    )
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`;")
        conn.commit()
        print(f"[OK] Ensured database exists: {DB_NAME}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def ensure_table_exists(cursor) -> None:
    """
    ✅ Create strategies table if it doesn't exist.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS strategies (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE,
            class_name VARCHAR(255) NOT NULL,
            python_code LONGTEXT,
            approved BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NULL,
            approved_at TIMESTAMP NULL
        );
        """
    )
    print("[OK] Ensured table exists: strategies")


def connect_to_db():
    """
    Connect to the target DB_NAME after ensuring it exists.
    """
    return mysql.connector.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
    )


def main():
    mode = (MODE or "").strip().lower()
    if mode not in ("upload", "update"):
        raise ValueError("MODE must be 'upload' or 'update'")

    if not (STRATEGY_NAME or "").strip():
        raise ValueError("STRATEGY_NAME is required")
    if not (CLASS_NAME or "").strip():
        raise ValueError("CLASS_NAME is required (do not leave empty)")
    if not (FILE_PATH or "").strip():
        raise ValueError("FILE_PATH is required")

    python_code = read_file(FILE_PATH)
    validate_python_syntax(python_code, FILE_PATH)

    # ✅ Make sure DB exists
    ensure_database_exists()

    # ✅ Connect to DB and ensure table
    conn = connect_to_db()
    cursor = conn.cursor()

    try:
        ensure_table_exists(cursor)
        conn.commit()

        now = datetime.utcnow()
        approved_int = int(bool(APPROVED))
        approved_at = now if APPROVED else None

        # -------------------------
        # UPDATE MODE
        # -------------------------
        if mode == "update":
            cursor.execute(
                "SELECT id FROM strategies WHERE name=%s LIMIT 1",
                (STRATEGY_NAME,),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError(
                    f"[ERROR] Strategy '{STRATEGY_NAME}' does NOT exist. "
                    f"Change MODE to 'upload' first."
                )

            cursor.execute(
                """
                UPDATE strategies
                SET class_name=%s,
                    python_code=%s,
                    approved=%s,
                    updated_at=%s,
                    approved_at=%s
                WHERE name=%s
                """,
                (
                    CLASS_NAME,          # ✅ always written
                    python_code,         # ✅ always written
                    approved_int,
                    now,
                    approved_at,
                    STRATEGY_NAME,
                ),
            )
            conn.commit()
            print(
                f"[UPDATE OK] '{STRATEGY_NAME}' updated in {DB_NAME}.strategies "
                f"(class_name='{CLASS_NAME}', approved={bool(APPROVED)})"
            )
            return

        # -------------------------
        # UPLOAD MODE (UPSERT)
        # -------------------------
        cursor.execute(
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
            (
                STRATEGY_NAME,
                CLASS_NAME,      # ✅ always written
                python_code,
                approved_int,
                now,
                now,
                approved_at,
            ),
        )
        conn.commit()
        print(
            f"[UPLOAD OK] '{STRATEGY_NAME}' saved into {DB_NAME}.strategies "
            f"(class_name='{CLASS_NAME}', approved={bool(APPROVED)})"
        )

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
