"""
database.py — v5.0
Architecture multi-tenant : base MySQL unique avec company_id partout.
Fallback SQLite pour développement local (DB_ENGINE=sqlite).

Nouveautés v5 :
  • token_blacklist — révocation JWT côté serveur
  • revoked_before   — révocation en masse par utilisateur (deactivate_user)
  • cleanup_blacklist() — nettoyage automatique des entrées expirées
"""
import sqlite3, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import get_settings
from db_driver import mysql_conn, mysql_enabled

cfg = get_settings()

# ═══════════════════════════════════════════════════════════════════════════════
# CONNEXIONS
# ═══════════════════════════════════════════════════════════════════════════════

SHARED_DB = cfg.DATA_DIR / "shared.db"


def _shared_conn():
    if mysql_enabled():
        return mysql_conn()
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SHARED_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn(company_id: str):
    """Connexion scopée à la boutique (tenant)."""
    if mysql_enabled():
        return mysql_conn(company_id or None)
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_tenant_path(company_id)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_tenant_schema(conn)
    return conn


def _tenant_path(company_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_id)
    return cfg.DATA_DIR / f"erp_{safe}.db"


# ═══════════════════════════════════════════════════════════════════════════════
# INITIALISATION DES SCHÉMAS
# ═══════════════════════════════════════════════════════════════════════════════

def init_shared_db():
    """Crée / met à jour la base partagée (companies, users, subscriptions…)."""
    with _shared_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                email           TEXT NOT NULL UNIQUE,
                secret_key      TEXT NOT NULL DEFAULT '',
                logo_url        TEXT,
                commercial_name TEXT,
                rccm            TEXT,
                ifu             TEXT,
                address         TEXT,
                phone           TEXT,
                contact_email   TEXT,
                is_vat_registered INTEGER NOT NULL DEFAULT 1,
                mecef_token     TEXT,
                low_stock_threshold INTEGER NOT NULL DEFAULT 10,
                plan            TEXT NOT NULL DEFAULT 'free',
                status          TEXT NOT NULL DEFAULT 'active',
                created_at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id              TEXT PRIMARY KEY,
                company_id      TEXT NOT NULL,
                email           TEXT NOT NULL UNIQUE,
                password_hash   TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'employee',
                is_active       INTEGER NOT NULL DEFAULT 1,
                email_verified  INTEGER NOT NULL DEFAULT 0,
                revoked_before  TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            );
            CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
            CREATE INDEX IF NOT EXISTS idx_users_email   ON users(email);

            CREATE TABLE IF NOT EXISTS subscriptions (
                id                     TEXT PRIMARY KEY,
                company_id             TEXT NOT NULL UNIQUE,
                plan                   TEXT NOT NULL DEFAULT 'free',
                status                 TEXT NOT NULL DEFAULT 'active',
                start_date             TEXT NOT NULL,
                end_date               TEXT,
                stripe_subscription_id TEXT,
                stripe_customer_id     TEXT,
                updated_at             TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sub_company ON subscriptions(company_id);

            CREATE TABLE IF NOT EXISTS invite_tokens (
                token       TEXT PRIMARY KEY,
                company_id  TEXT NOT NULL,
                email       TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'employee',
                expires_at  TEXT NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                email       TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                email       TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS token_blacklist (
                jti         TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bl_expires ON token_blacklist(expires_at);
            CREATE INDEX IF NOT EXISTS idx_bl_user    ON token_blacklist(user_id);
        """)

        # ── Migrations de colonnes (idempotentes) ──────────────────────────
        if not mysql_enabled():
            _sqlite_add_column_if_missing(conn, "companies", "secret_key",      "TEXT NOT NULL DEFAULT ''")
            _sqlite_add_column_if_missing(conn, "companies", "logo_url",         "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "commercial_name",  "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "rccm",             "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "ifu",              "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "address",          "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "phone",            "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "contact_email",    "TEXT")
            _sqlite_add_column_if_missing(conn, "users", "email_verified",  "INTEGER NOT NULL DEFAULT 0")
            _sqlite_add_column_if_missing(conn, "users", "revoked_before",  "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "is_vat_registered", "INTEGER NOT NULL DEFAULT 1")
            _sqlite_add_column_if_missing(conn, "companies", "mecef_token",       "TEXT")
            _sqlite_add_column_if_missing(conn, "companies", "low_stock_threshold", "INTEGER NOT NULL DEFAULT 10")


        else:
            # MySQL migrations (colonnes ajoutees apres creation initiale de la table)
            _mysql_add_column_if_missing(conn, "companies", "secret_key",      "TEXT NOT NULL DEFAULT ''")
            _mysql_add_column_if_missing(conn, "companies", "logo_url",         "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "commercial_name",  "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "rccm",             "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "ifu",              "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "address",          "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "phone",            "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "contact_email",    "TEXT")
            _mysql_add_column_if_missing(conn, "users",     "email_verified",   "INTEGER NOT NULL DEFAULT 0")
            _mysql_add_column_if_missing(conn, "users",     "revoked_before",   "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "is_vat_registered","INTEGER NOT NULL DEFAULT 1")
            _mysql_add_column_if_missing(conn, "companies", "mecef_token",      "TEXT")
            _mysql_add_column_if_missing(conn, "companies", "low_stock_threshold", "INTEGER NOT NULL DEFAULT 10")

def _sqlite_add_column_if_missing(conn, table: str, column: str, definition: str):
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _mysql_add_column_if_missing(conn, table: str, column: str, definition: str):
    """Idempotent ALTER TABLE pour MySQL (verifie information_schema)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    ).fetchone()
    count = list(row.values())[0] if isinstance(row, dict) else row[0]
    if count == 0:
        conn.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")



def _ensure_tenant_schema(conn):
    """Tables produits/ventes pour SQLite (idem MYSQL_TENANT_SCHEMA dans db_driver)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id                    TEXT PRIMARY KEY,
            sku                   TEXT,
            name                  TEXT NOT NULL,
            description           TEXT,
            price                 REAL NOT NULL,
            stock                 INTEGER NOT NULL DEFAULT 0,
            image_url             TEXT,
            reference_image_url   TEXT,
            reference_image_hash  TEXT,
            consumer_code         TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prod_consumer ON products(consumer_code);
        CREATE INDEX IF NOT EXISTS idx_prod_sku      ON products(sku);

        CREATE TABLE IF NOT EXISTS sales (
            id                 TEXT PRIMARY KEY,
            reference          TEXT NOT NULL,
            source             TEXT DEFAULT 'dashboard',
            items              TEXT NOT NULL,
            total              REAL NOT NULL DEFAULT 0,
            customer           TEXT,
            note               TEXT,
            created_by_user_id TEXT,
            created_by_email   TEXT,
            created_by_role    TEXT,
            created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sales_user ON sales(created_by_user_id);

        CREATE TABLE IF NOT EXISTS audit_logs (
            id           TEXT PRIMARY KEY,
            user_id      TEXT,
            user_email   TEXT,
            user_role    TEXT,
            action       TEXT NOT NULL,
            object_type  TEXT NOT NULL,
            object_id    TEXT,
            object_label TEXT,
            details      TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_user    ON audit_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_object  ON audit_logs(object_type, object_id);

        CREATE TABLE IF NOT EXISTS auth_codes (
            id         TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            code       TEXT NOT NULL UNIQUE,
            status     TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_codes_product ON auth_codes(product_id);
        CREATE INDEX IF NOT EXISTS idx_codes_code    ON auth_codes(code);

        CREATE TABLE IF NOT EXISTS verifications (
            id           TEXT PRIMARY KEY,
            code_id      TEXT,
            product_id   TEXT NOT NULL,
            verified_at  TEXT NOT NULL,
            latitude     REAL,
            longitude    REAL,
            city         TEXT,
            country      TEXT,
            ip_address   TEXT,
            user_agent   TEXT,
            code_value   TEXT,
            attempt_type TEXT NOT NULL DEFAULT 'valid',
            is_valid     INTEGER NOT NULL DEFAULT 1,
            is_fraud     INTEGER NOT NULL DEFAULT 0,
            note         TEXT,
            FOREIGN KEY (code_id) REFERENCES auth_codes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_verif_product ON verifications(product_id);
        CREATE INDEX IF NOT EXISTS idx_verif_code    ON verifications(code_id);
    """)


def init_tenant_db(company_id: str):
    """Appelé à la création d'une boutique pour pré-créer les tables (MySQL)."""
    if mysql_enabled():
        with get_conn(company_id) as conn:
            conn.executescript("CREATE TABLE IF NOT EXISTS products")
    else:
        with get_conn(company_id):
            pass  # _ensure_tenant_schema est appelé dans get_conn SQLite


def tenant_ids() -> list[str]:
    if mysql_enabled():
        with _shared_conn() as conn:
            rows = conn.execute("SELECT id FROM companies WHERE status='active'").fetchall()
        return [row["id"] for row in rows]
    return [f.stem[4:] for f in cfg.DATA_DIR.glob("erp_*.db")]


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN BLACKLIST — révocation JWT
# ═══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def blacklist_token(jti: str, user_id: str, expires_at: str):
    """Blackliste un token JWT par son JTI."""
    with _shared_conn() as conn:
        if mysql_enabled():
            conn.execute("""
                INSERT INTO token_blacklist (jti, user_id, expires_at, created_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE user_id=user_id
            """, (jti, user_id, expires_at, _now_iso()))
        else:
            conn.execute("""
                INSERT OR IGNORE INTO token_blacklist (jti, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
            """, (jti, user_id, expires_at, _now_iso()))


def is_token_blacklisted(jti: str) -> bool:
    """Vérifie si un JTI est dans la blacklist."""
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT jti FROM token_blacklist WHERE jti=?", (jti,)
        ).fetchone()
    return row is not None


def revoke_user_tokens(user_id: str):
    """
    Révoque tous les tokens actifs d'un utilisateur en enregistrant
    le timestamp courant dans revoked_before.
    Tout token émis AVANT ce timestamp sera rejeté.
    """
    now = _now_iso()
    with _shared_conn() as conn:
        conn.execute(
            "UPDATE users SET revoked_before=? WHERE id=?",
            (now, user_id),
        )


def get_user_revoked_before(user_id: str) -> str | None:
    """Retourne le timestamp de révocation globale d'un user (ou None)."""
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT revoked_before FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return row["revoked_before"] if isinstance(row, dict) else row[0]


def cleanup_blacklist():
    """Supprime les entrées expirées de la blacklist. À appeler périodiquement."""
    now = _now_iso()
    with _shared_conn() as conn:
        conn.execute(
            "DELETE FROM token_blacklist WHERE expires_at < ?", (now,)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# COMPANIES
# ═══════════════════════════════════════════════════════════════════════════════

def get_company(company_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None


def get_company_by_email(email: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE email=?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def get_company_by_id_or_email(identifier: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE lower(id)=? OR lower(email)=?",
            (identifier.lower(), identifier.lower()),
        ).fetchone()
    return dict(row) if row else None


def get_company_by_secret_key(secret_key: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE secret_key=? AND status='active'",
            (secret_key,),
        ).fetchone()
    return dict(row) if row else None


def insert_company(c: dict):
    # Valeurs par défaut nécessaires pour les boutiques créées par une ancienne
    # migration, un script de démonstration ou une nouvelle installation.
    c = dict(c)
    c.setdefault("is_vat_registered", 1)
    c.setdefault("mecef_token", None)
    c.setdefault("low_stock_threshold", 10)
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO companies ("
            "id,name,email,secret_key,logo_url,commercial_name,rccm,ifu,address,phone,contact_email,is_vat_registered,mecef_token,low_stock_threshold,plan,status,created_at"
            ") VALUES ("
            ":id,:name,:email,:secret_key,:logo_url,:commercial_name,:rccm,:ifu,:address,:phone,:contact_email,:is_vat_registered,:mecef_token,:low_stock_threshold,:plan,:status,:created_at"
            ")",
            c,
        )


def all_companies() -> list:
    with _shared_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "s.plan as sub_plan, s.status as sub_status, "
            "s.start_date as subscription_start_date, "
            "s.end_date as subscription_end_date, "
            "s.stripe_subscription_id as fedapay_transaction_id, "
            "s.stripe_customer_id as payment_customer_id "
            "FROM companies c LEFT JOIN subscriptions s ON c.id=s.company_id "
            "ORDER BY c.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_company_status(company_id: str, status: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE companies SET status=? WHERE id=?", (status, company_id))


def update_company_secret_key(company_id: str, secret_key: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE companies SET secret_key=? WHERE id=?", (secret_key, company_id))


def update_company_logo(company_id: str, logo_url: str | None):
    with _shared_conn() as conn:
        conn.execute("UPDATE companies SET logo_url=? WHERE id=?", (logo_url, company_id))


def update_company_profile(company_id: str, payload: dict):
    with _shared_conn() as conn:
        conn.execute(
            "UPDATE companies SET "
            "name=:name, commercial_name=:commercial_name, rccm=:rccm, ifu=:ifu, "
            "address=:address, phone=:phone, contact_email=:contact_email, "
            "is_vat_registered=:is_vat_registered, mecef_token=:mecef_token, "
            "low_stock_threshold=:low_stock_threshold "
            "WHERE id=:company_id",
            {
                "company_id": company_id,
                "name": payload["name"],
                "commercial_name": payload["commercial_name"],
                "rccm": payload["rccm"],
                "ifu": payload["ifu"],
                "address": payload["address"],
                "phone": payload["phone"],
                "contact_email": payload["contact_email"],
                "is_vat_registered": payload.get("is_vat_registered", 1),
                "mecef_token": payload.get("mecef_token"),
                "low_stock_threshold": payload.get("low_stock_threshold", 10),
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_user_by_email(email: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT u.*, c.name as company_name, c.status as company_status, "
            "c.logo_url as company_logo_url, "
            "c.commercial_name, c.rccm, c.ifu, c.address, c.phone, c.contact_email "
            "FROM users u JOIN companies c ON u.company_id=c.id "
            "WHERE u.email=?", (email.lower(),)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT u.*, c.name as company_name, c.status as company_status, "
            "c.logo_url as company_logo_url, "
            "c.commercial_name, c.rccm, c.ifu, c.address, c.phone, c.contact_email "
            "FROM users u JOIN companies c ON u.company_id=c.id "
            "WHERE u.id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def insert_user(u: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO users (id,company_id,email,password_hash,role,is_active,email_verified,created_at) "
            "VALUES (:id,:company_id,:email,:password_hash,:role,:is_active,:email_verified,:created_at)", u
        )


def update_user_password(user_id: str, password_hash: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))


def set_user_email_verified(user_id: str, is_verified: int = 1):
    with _shared_conn() as conn:
        conn.execute("UPDATE users SET email_verified=? WHERE id=?", (is_verified, user_id))


def list_users_for_company(company_id: str) -> list:
    with _shared_conn() as conn:
        rows = conn.execute(
            "SELECT id,company_id,email,role,is_active,created_at "
            "FROM users WHERE company_id=? ORDER BY created_at",
            (company_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_users_for_company(company_id: str) -> int:
    with _shared_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE company_id=? AND is_active=1",
            (company_id,)
        ).fetchone()[0]


def deactivate_user(user_id: str):
    """Désactive un user ET révoque tous ses tokens actifs."""
    with _shared_conn() as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
    revoke_user_tokens(user_id)


def activate_user(user_id: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))


def get_primary_user_for_company(company_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT u.*, c.name as company_name, c.status as company_status, "
            "c.logo_url as company_logo_url, "
            "c.commercial_name, c.rccm, c.ifu, c.address, c.phone, c.contact_email "
            "FROM users u JOIN companies c ON u.company_id=c.id "
            "WHERE u.company_id=? AND u.is_active=1 "
            "ORDER BY CASE u.role "
            "WHEN 'admin' THEN 0 "
            "WHEN 'manager' THEN 1 "
            "WHEN 'employee' THEN 2 "
            "WHEN 'superadmin' THEN 3 "
            "ELSE 9 END, u.created_at ASC "
            "LIMIT 1",
            (company_id,),
        ).fetchone()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_subscription(company_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE company_id=?", (company_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_subscription(s: dict):
    with _shared_conn() as conn:
        if mysql_enabled():
            conn.execute("""
                INSERT INTO subscriptions
                    (id,company_id,plan,status,start_date,end_date,
                     stripe_subscription_id,stripe_customer_id,updated_at)
                VALUES
                    (:id,:company_id,:plan,:status,:start_date,:end_date,
                     :stripe_subscription_id,:stripe_customer_id,:updated_at)
                ON DUPLICATE KEY UPDATE
                    plan=VALUES(plan), status=VALUES(status),
                    end_date=VALUES(end_date),
                    stripe_subscription_id=VALUES(stripe_subscription_id),
                    stripe_customer_id=VALUES(stripe_customer_id),
                    updated_at=VALUES(updated_at)
            """, s)
        else:
            conn.execute("""
                INSERT INTO subscriptions
                    (id,company_id,plan,status,start_date,end_date,
                     stripe_subscription_id,stripe_customer_id,updated_at)
                VALUES
                    (:id,:company_id,:plan,:status,:start_date,:end_date,
                     :stripe_subscription_id,:stripe_customer_id,:updated_at)
                ON CONFLICT(company_id) DO UPDATE SET
                    plan=excluded.plan, status=excluded.status,
                    end_date=excluded.end_date,
                    stripe_subscription_id=excluded.stripe_subscription_id,
                    stripe_customer_id=excluded.stripe_customer_id,
                    updated_at=excluded.updated_at
            """, s)


def get_active_plan(company_id: str) -> str:
    sub = get_subscription(company_id)
    if not sub or sub["status"] not in ("active", "trialing"):
        return "free"
    # Un abonnement payant terminé ne doit plus conserver ses quotas.
    end_date = sub.get("end_date")
    if not end_date and sub.get("plan") != "free" and sub.get("start_date"):
        # Abonnements historiques : même règle de 31 jours que FedaPay.
        try:
            end_date = (datetime.fromisoformat(sub["start_date"]) + timedelta(days=31)).isoformat()
        except (TypeError, ValueError):
            end_date = None
    if end_date:
        try:
            if datetime.now(tz=timezone.utc) >= datetime.fromisoformat(end_date):
                return "free"
        except (TypeError, ValueError):
            pass
    return sub["plan"]


# ═══════════════════════════════════════════════════════════════════════════════
# TOKENS (invitations, email, password reset)
# ═══════════════════════════════════════════════════════════════════════════════

def insert_invite(invite: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO invite_tokens (token,company_id,email,role,expires_at,used) "
            "VALUES (:token,:company_id,:email,:role,:expires_at,:used)", invite
        )


def get_invite(token: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invite_tokens WHERE token=? AND used=0", (token,)
        ).fetchone()
    return dict(row) if row else None


def mark_invite_used(token: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE invite_tokens SET used=1 WHERE token=?", (token,))


def insert_email_verification_token(data: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO email_verification_tokens (token,user_id,email,expires_at,used) "
            "VALUES (:token,:user_id,:email,:expires_at,:used)", data
        )


def get_email_verification_token(token: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_verification_tokens WHERE token=? AND used=0", (token,)
        ).fetchone()
    return dict(row) if row else None


def mark_email_verification_token_used(token: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE email_verification_tokens SET used=1 WHERE token=?", (token,))


def insert_password_reset_token(data: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (token,user_id,email,expires_at,used) "
            "VALUES (:token,:user_id,:email,:expires_at,:used)", data
        )


def get_password_reset_token(token: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token=? AND used=0", (token,)
        ).fetchone()
    return dict(row) if row else None


def mark_password_reset_token_used(token: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))


def clear_password_reset_tokens_for_user(user_id: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=?", (user_id,))


# ═══════════════════════════════════════════════════════════════════════════════
# STATS SUPER-ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

def global_stats() -> dict:
    with _shared_conn() as conn:
        n_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE status='active'").fetchone()[0]
        n_users     = conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
    total_revenue = 0.0
    total_sales   = 0
    if mysql_enabled():
        # Connexion sans scoping tenant pour agréger toutes les boutiques
        with mysql_conn(None) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS rev FROM sales"
            ).fetchone()
            if row:
                total_sales   = int(row["cnt"])
                total_revenue = float(row["rev"])
    else:
        for db_file in cfg.DATA_DIR.glob("erp_*.db"):
            try:
                c = sqlite3.connect(str(db_file))
                row = c.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM sales").fetchone()
                total_sales   += row[0]
                total_revenue += row[1]
                c.close()
            except Exception:
                pass
    return {
        "companies":     n_companies,
        "users":         n_users,
        "total_sales":   total_sales,
        "total_revenue": total_revenue,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUITS
# ═══════════════════════════════════════════════════════════════════════════════

def _blank_product(p: dict) -> dict:
    defaults = {"image_url": None, "reference_image_url": None,
                "reference_image_hash": None, "consumer_code": None}
    return {**defaults, **p}


def all_products(company_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def page_products(company_id: str, page: int = 1, per_page: int = 20, query: str = "") -> tuple[list, int]:
    """Retourne uniquement la page demandée, filtrée directement en base."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    query = query.strip()
    where = ""
    params: list = []
    if query:
        where = " WHERE lower(name) LIKE ? OR lower(COALESCE(sku, '')) LIKE ?"
        needle = f"%{query.lower()}%"
        params = [needle, needle]
    with get_conn(company_id) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM products{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM products{where} ORDER BY name, id LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def count_products(company_id: str) -> int:
    with get_conn(company_id) as conn:
        return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]


def get_product(company_id: str, product_id: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return dict(row) if row else None


def get_product_by_sku(company_id: str, sku: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    return dict(row) if row else None


def get_product_by_consumer_code(company_id: str, code: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM products WHERE consumer_code=?", (code,)).fetchone()
    return dict(row) if row else None


def get_product_by_name(company_id: str, name: str) -> dict | None:
    normalized = name.strip().lower()
    with get_conn(company_id) as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE lower(trim(name))=?", (normalized,)
        ).fetchone()
    return dict(row) if row else None


def insert_product(company_id: str, p: dict):
    p = _blank_product(p)
    with get_conn(company_id) as conn:
        conn.execute("""
            INSERT INTO products
                (id,sku,name,description,price,stock,
                 image_url,reference_image_url,reference_image_hash,
                 consumer_code,created_at,updated_at)
            VALUES
                (:id,:sku,:name,:description,:price,:stock,
                 :image_url,:reference_image_url,:reference_image_hash,
                 :consumer_code,:created_at,:updated_at)
        """, p)


def update_product_full(company_id: str, p: dict):
    p = _blank_product(p)
    with get_conn(company_id) as conn:
        conn.execute("""
            UPDATE products SET
                sku=:sku, name=:name, description=:description,
                price=:price, stock=:stock,
                image_url=:image_url,
                reference_image_url=:reference_image_url,
                reference_image_hash=:reference_image_hash,
                consumer_code=:consumer_code,
                updated_at=:updated_at
            WHERE id=:id
        """, p)


def update_product_image(company_id: str, product_id: str,
                         image_url, ref_image_url, ref_image_hash, updated_at):
    with get_conn(company_id) as conn:
        conn.execute("""
            UPDATE products SET
                image_url=?, reference_image_url=?,
                reference_image_hash=?, updated_at=?
            WHERE id=?
        """, (image_url, ref_image_url, ref_image_hash, updated_at, product_id))


def update_stock(company_id: str, product_id: str, new_stock: int, updated_at: str):
    with get_conn(company_id) as conn:
        conn.execute(
            "UPDATE products SET stock=?, updated_at=? WHERE id=?",
            (new_stock, updated_at, product_id)
        )


def delete_product(company_id: str, product_id: str):
    with get_conn(company_id) as conn:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))


def delete_products(company_id: str, product_ids: list[str]) -> list[dict]:
    """Supprime plusieurs produits et retourne ceux réellement supprimés."""
    ids = list(dict.fromkeys(product_id.strip() for product_id in product_ids if product_id.strip()))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with get_conn(company_id) as conn:
        rows = conn.execute(
            f"SELECT id, name, sku FROM products WHERE id IN ({placeholders})", ids
        ).fetchall()
        conn.execute(f"DELETE FROM products WHERE id IN ({placeholders})", ids)
    return [dict(row) for row in rows]


def delete_company_permanently(company_id: str) -> bool:
    """Efface les données applicatives d'une boutique et ses relations.

    Les données de paiement déjà détenues par un prestataire externe et les
    sauvegardes d'infrastructure ne sont pas modifiées ici.
    """
    if not get_company(company_id):
        return False

    # Tables locataires : une base par boutique en SQLite, ou des lignes
    # scopées par company_id en MySQL.
    tenant_tables = ("verifications", "auth_codes", "sales", "audit_logs", "products")
    if mysql_enabled():
        with get_conn(company_id) as conn:
            for table in tenant_tables:
                conn.execute(f"DELETE FROM {table}")
    else:
        # Nettoyer les tables avant de retirer le fichier. Sous Windows, un
        # handle SQLite extérieur peut retarder unlink(), sans empêcher
        # l'effacement immédiat des données métier.
        tenant_conn = get_conn(company_id)
        try:
            for table in tenant_tables:
                tenant_conn.execute(f"DELETE FROM {table}")
            tenant_conn.commit()
        finally:
            tenant_conn.close()

    # Les jetons référencent des utilisateurs : il faut les retirer avant les
    # comptes, puis supprimer les relations de la boutique avant sa fiche.
    with _shared_conn() as conn:
        rows = conn.execute("SELECT id FROM users WHERE company_id=?", (company_id,)).fetchall()
        user_ids = [row["id"] for row in rows]
        if user_ids:
            placeholders = ",".join("?" for _ in user_ids)
            for table in ("email_verification_tokens", "password_reset_tokens", "token_blacklist"):
                conn.execute(f"DELETE FROM {table} WHERE user_id IN ({placeholders})", tuple(user_ids))
        conn.execute("DELETE FROM invite_tokens WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM subscriptions WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM users WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id=?", (company_id,))

    if not mysql_enabled():
        # La base SQLite locataire contient produits, ventes et journaux :
        # supprimer le fichier et ses journaux WAL retire toutes ses traces.
        tenant_db = _tenant_path(company_id)
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{tenant_db}{suffix}")
            if path.exists():
                try:
                    path.unlink()
                except PermissionError:
                    # Les tables ont déjà été vidées ci-dessus. Le fichier
                    # restant ne contient donc plus de données de boutique.
                    pass
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# VENTES
# ═══════════════════════════════════════════════════════════════════════════════

def _sale_row(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("items"), str):
        d["items"] = json.loads(d["items"])
    return d


def all_sales(company_id: str, created_by_user_id: str | None = None) -> list:
    with get_conn(company_id) as conn:
        if created_by_user_id:
            rows = conn.execute(
                "SELECT * FROM sales WHERE created_by_user_id=? ORDER BY created_at DESC",
                (created_by_user_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sales ORDER BY created_at DESC").fetchall()
    return [_sale_row(r) for r in rows]


def page_sales(
    company_id: str,
    created_by_user_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list, int]:
    """Charge une page de ventes directement depuis la base."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    clauses, params = [], []
    if created_by_user_id:
        clauses.append("created_by_user_id=?")
        params.append(created_by_user_id)
    if period_start:
        clauses.append("created_at>=?")
        params.append(period_start)
    if period_end:
        clauses.append("created_at<?")
        params.append(period_end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn(company_id) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM sales{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM sales{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    return [_sale_row(row) for row in rows], int(total)


def count_sales_this_month(company_id: str) -> int:
    month_start = datetime.now().strftime("%Y-%m-01")
    with get_conn(company_id) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM sales WHERE created_at >= ?", (month_start,)
        ).fetchone()[0]


def get_sale(company_id: str, sale_id: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    return _sale_row(row)


def insert_sale(company_id: str, s: dict):
    items_json = json.dumps(s.get("items", []), ensure_ascii=False)
    with get_conn(company_id) as conn:
        conn.execute("""
            INSERT INTO sales
                (id,reference,source,items,total,customer,note,
                 created_by_user_id,created_by_email,created_by_role,created_at)
            VALUES
                (:id,:reference,:source,:items,:total,:customer,:note,
                 :created_by_user_id,:created_by_email,:created_by_role,:created_at)
        """, {**s, "items": items_json})


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT LOGS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_audit_log(company_id: str, entry: dict):
    payload = {
        **entry,
        "details": json.dumps(entry.get("details") or {}, ensure_ascii=False),
    }
    with get_conn(company_id) as conn:
        conn.execute("""
            INSERT INTO audit_logs
                (id,user_id,user_email,user_role,action,object_type,
                 object_id,object_label,details,created_at)
            VALUES
                (:id,:user_id,:user_email,:user_role,:action,:object_type,
                 :object_id,:object_label,:details,:created_at)
        """, payload)


def list_audit_logs(company_id: str, user_id: str | None = None, limit: int = 100) -> list:
    limit = max(1, min(limit, 500))
    with get_conn(company_id) as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except Exception:
            item["details"] = {}
        result.append(item)
    return result


def page_audit_logs(company_id: str, user_id: str | None = None, page: int = 1, per_page: int = 20) -> tuple[list, int]:
    """Pagination SQL du journal d'activité : aucune ligne inutile n'est envoyée."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    where, params = (" WHERE user_id=?", [user_id]) if user_id else ("", [])
    with get_conn(company_id) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_logs{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM audit_logs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except Exception:
            item["details"] = {}
        result.append(item)
    return result, int(total)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH CODES & VÉRIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_auth_code(company_id: str, c: dict):
    with get_conn(company_id) as conn:
        conn.execute(
            "INSERT INTO auth_codes (id,product_id,code,status,created_at) "
            "VALUES (:id,:product_id,:code,:status,:created_at)", c
        )


def get_auth_code_by_value(company_id: str, code: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM auth_codes WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def get_auth_code_by_id(company_id: str, code_id: str) -> dict | None:
    with get_conn(company_id) as conn:
        row = conn.execute("SELECT * FROM auth_codes WHERE id=?", (code_id,)).fetchone()
    return dict(row) if row else None


def mark_code_used(company_id: str, code_id: str):
    with get_conn(company_id) as conn:
        conn.execute("UPDATE auth_codes SET status='used' WHERE id=?", (code_id,))


def get_codes_for_product(company_id: str, product_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("""
            SELECT a.*, v.verified_at, v.latitude, v.longitude, v.city, v.country,
                   v.is_fraud, v.attempt_type, v.note
            FROM auth_codes a
            LEFT JOIN verifications v ON v.code_id = a.id
            WHERE a.product_id=?
            ORDER BY a.created_at DESC
        """, (product_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_verification(company_id: str, v: dict):
    with get_conn(company_id) as conn:
        conn.execute("""
            INSERT INTO verifications
                (id,code_id,product_id,verified_at,latitude,longitude,
                 city,country,ip_address,user_agent,code_value,attempt_type,
                 is_valid,is_fraud,note)
            VALUES
                (:id,:code_id,:product_id,:verified_at,:latitude,:longitude,
                 :city,:country,:ip_address,:user_agent,:code_value,:attempt_type,
                 :is_valid,:is_fraud,:note)
        """, v)


def all_verifications(company_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("""
            SELECT v.*, p.name as product_name, a.code
            FROM verifications v
            JOIN products p ON v.product_id = p.id
            LEFT JOIN auth_codes a ON v.code_id = a.id
            ORDER BY v.verified_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_verification_stats(company_id: str) -> dict:
    with get_conn(company_id) as conn:
        total = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        by_product = conn.execute("""
            SELECT p.name, p.id, COUNT(v.id) as count
            FROM products p LEFT JOIN verifications v ON p.id=v.product_id
            GROUP BY p.id ORDER BY count DESC LIMIT 10
        """).fetchall()
        by_country = conn.execute("""
            SELECT country, COUNT(*) as count FROM verifications
            WHERE country IS NOT NULL GROUP BY country ORDER BY count DESC
        """).fetchall()
        fraud_attempts = conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE is_fraud=1"
        ).fetchone()[0]
    return {
        "total": total,
        "by_product": [dict(r) for r in by_product],
        "by_country": [dict(r) for r in by_country],
        "fraud_attempts": fraud_attempts,
    }


def auth_code_aggregate_stats(company_id: str) -> dict:
    with get_conn(company_id) as conn:
        total_codes = conn.execute("SELECT COUNT(*) FROM auth_codes").fetchone()[0]
        used_codes  = conn.execute("SELECT COUNT(*) FROM auth_codes WHERE status='used'").fetchone()[0]
        total_verif = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        fake = conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE is_fraud=1"
        ).fetchone()[0]
    return {
        "total_codes":         total_codes,
        "used_codes":          used_codes,
        "total_verifications": total_verif,
        "fake_attempts":       fake,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# INIT AU DÉMARRAGE
# ═══════════════════════════════════════════════════════════════════════════════
init_shared_db()
if mysql_enabled():
    print(f"[DB] MySQL -> {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}/{cfg.MYSQL_DATABASE}")
else:
    print(f"[DB] SQLite partagee -> {SHARED_DB}")
