"""
Migre les donnees SQLite existantes vers MySQL.

Prerequis:
- Creer la base MySQL et l'utilisateur.
- Configurer .env avec DB_ENGINE=mysql et MYSQL_*.
- Lancer: python migrate_sqlite_to_mysql.py
"""
import sqlite3
from pathlib import Path
from typing import Any

from config import get_settings
from db_driver import mysql_conn


cfg = get_settings()


SHARED_TABLES = {
    "companies": {
        "pk": ["id"],
        "columns": [
            "id", "name", "email", "secret_key", "logo_url",
            "commercial_name", "rccm", "ifu", "address", "phone",
            "contact_email", "plan", "status", "created_at",
        ],
        "defaults": {"secret_key": "", "plan": "free", "status": "active"},
    },
    "users": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "email", "password_hash", "role",
            "is_active", "email_verified", "created_at",
        ],
        "defaults": {"role": "employee", "is_active": 1, "email_verified": 0},
    },
    "subscriptions": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "plan", "status", "start_date", "end_date",
            "stripe_subscription_id", "stripe_customer_id", "updated_at",
        ],
        "defaults": {"plan": "free", "status": "active"},
    },
    "invite_tokens": {
        "pk": ["token"],
        "columns": ["token", "company_id", "email", "role", "expires_at", "used"],
        "defaults": {"role": "employee", "used": 0},
    },
    "email_verification_tokens": {
        "pk": ["token"],
        "columns": ["token", "user_id", "email", "expires_at", "used"],
        "defaults": {"used": 0},
    },
    "password_reset_tokens": {
        "pk": ["token"],
        "columns": ["token", "user_id", "email", "expires_at", "used"],
        "defaults": {"used": 0},
    },
}


TENANT_TABLES = {
    "products": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "sku", "name", "description", "price",
            "stock", "image_url", "reference_image_url",
            "reference_image_hash", "consumer_code", "created_at", "updated_at",
        ],
        "defaults": {"stock": 0},
    },
    "sales": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "reference", "source", "items", "total",
            "customer", "note", "created_by_user_id", "created_by_email",
            "created_by_role", "created_at",
        ],
        "defaults": {"source": "dashboard", "total": 0},
    },
    "audit_logs": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "user_id", "user_email", "user_role",
            "action", "object_type", "object_id", "object_label",
            "details", "created_at",
        ],
        "defaults": {"details": "{}"},
    },
    "auth_codes": {
        "pk": ["id"],
        "columns": ["id", "company_id", "product_id", "code", "status", "created_at"],
        "defaults": {"status": "active"},
    },
    "verifications": {
        "pk": ["id"],
        "columns": [
            "id", "company_id", "code_id", "product_id", "verified_at",
            "latitude", "longitude", "city", "country", "ip_address",
            "user_agent", "code_value", "attempt_type", "is_valid",
            "is_fraud", "note",
        ],
        "defaults": {"attempt_type": "valid", "is_valid": 1, "is_fraud": 0},
    },
}


def sqlite_rows(db_path: Path, table: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def normalize_row(row: dict[str, Any], columns: list[str], defaults: dict[str, Any], company_id: str | None = None):
    payload = {}
    for column in columns:
        if column == "company_id" and company_id is not None:
            payload[column] = company_id
        else:
            payload[column] = row.get(column, defaults.get(column))
    return payload


def upsert_many(conn, table: str, rows: list[dict[str, Any]], columns: list[str], pk: list[str]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(f"%({column})s" for column in columns)
    col_sql = ", ".join(columns)
    updates = ", ".join(
        f"{column}=VALUES({column})"
        for column in columns
        if column not in pk
    )
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
    if updates:
        sql += f" ON DUPLICATE KEY UPDATE {updates}"
    for row in rows:
        conn.execute(sql, row)
    return len(rows)


def migrate_shared(conn) -> None:
    shared_db = cfg.DATA_DIR / "shared.db"
    print(f"Base partagee: {shared_db}")
    for table, spec in SHARED_TABLES.items():
        rows = [
            normalize_row(row, spec["columns"], spec["defaults"])
            for row in sqlite_rows(shared_db, table)
        ]
        count = upsert_many(conn, table, rows, spec["columns"], spec["pk"])
        print(f"  {table}: {count}")


def migrate_tenants(conn) -> None:
    tenant_dbs = sorted(cfg.DATA_DIR.glob("erp_*.db"))
    print(f"Bases boutiques: {len(tenant_dbs)}")
    for db_path in tenant_dbs:
        company_id = db_path.stem[4:]
        print(f"  Boutique {company_id}: {db_path.name}")
        for table, spec in TENANT_TABLES.items():
            rows = [
                normalize_row(row, spec["columns"], spec["defaults"], company_id=company_id)
                for row in sqlite_rows(db_path, table)
            ]
            count = upsert_many(conn, table, rows, spec["columns"], spec["pk"])
            print(f"    {table}: {count}")


def main() -> int:
    if cfg.DB_ENGINE != "mysql":
        print("Erreur: mettez DB_ENGINE=mysql dans .env avant de lancer la migration.")
        return 1
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with mysql_conn() as conn:
        conn.executescript("CREATE TABLE IF NOT EXISTS companies")
        conn.executescript("CREATE TABLE IF NOT EXISTS products")
        migrate_shared(conn)
        migrate_tenants(conn)
    print("Migration terminee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
