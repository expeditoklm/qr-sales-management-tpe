"""
database.py — v4.0
Architecture multi-tenant : une base SQLite par boutique dans DATA_DIR/erp_{company_id}.db
La base partagée (shared.db) stocke : companies, users, subscriptions
"""
import sqlite3, json
from pathlib import Path
from functools import lru_cache
from config import get_settings

cfg = get_settings()

# ═══════════════════════════════════════════════════════════════════════════════
# BASE PARTAGÉE  (companies, users, subscriptions)
# ═══════════════════════════════════════════════════════════════════════════════

SHARED_DB = cfg.DATA_DIR / "shared.db"

def _shared_conn() -> sqlite3.Connection:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SHARED_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_shared_db():
    with _shared_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL UNIQUE,
                secret_key  TEXT NOT NULL DEFAULT '',
                plan        TEXT NOT NULL DEFAULT 'free',
                status      TEXT NOT NULL DEFAULT 'active',
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                company_id    TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'employee',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
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
        """)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "secret_key" not in columns:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN secret_key TEXT NOT NULL DEFAULT ''"
            )


# ─── Companies ────────────────────────────────────────────────────────────────

def get_company(company_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None

def get_company_by_email(email: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE email=?", (email.lower(),)).fetchone()
    return dict(row) if row else None

def get_company_by_secret_key(secret_key: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE secret_key=? AND status='active'",
            (secret_key,),
        ).fetchone()
    return dict(row) if row else None

def insert_company(c: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO companies (id,name,email,secret_key,plan,status,created_at) "
            "VALUES (:id,:name,:email,:secret_key,:plan,:status,:created_at)", c
        )

def all_companies() -> list:
    with _shared_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "s.plan as sub_plan, s.status as sub_status, "
            "s.start_date as subscription_start_date, "
            "s.end_date as subscription_end_date, "
            "s.stripe_subscription_id as stripe_subscription_id, "
            "s.stripe_customer_id as stripe_customer_id "
            "FROM companies c LEFT JOIN subscriptions s ON c.id=s.company_id "
            "ORDER BY c.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]

def update_company_status(company_id: str, status: str):
    with _shared_conn() as conn:
        conn.execute("UPDATE companies SET status=? WHERE id=?", (status, company_id))

def update_company_secret_key(company_id: str, secret_key: str):
    with _shared_conn() as conn:
        conn.execute(
            "UPDATE companies SET secret_key=? WHERE id=?",
            (secret_key, company_id),
        )


# ─── Users ────────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT u.*, c.name as company_name, c.status as company_status "
            "FROM users u JOIN companies c ON u.company_id=c.id "
            "WHERE u.email=?", (email.lower(),)
        ).fetchone()
    return dict(row) if row else None

def get_user_by_id(user_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT u.*, c.name as company_name, c.status as company_status "
            "FROM users u JOIN companies c ON u.company_id=c.id "
            "WHERE u.id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None

def insert_user(u: dict):
    with _shared_conn() as conn:
        conn.execute(
            "INSERT INTO users (id,company_id,email,password_hash,role,is_active,created_at) "
            "VALUES (:id,:company_id,:email,:password_hash,:role,:is_active,:created_at)", u
        )

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
    with _shared_conn() as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))


# ─── Subscriptions ────────────────────────────────────────────────────────────

def get_subscription(company_id: str) -> dict | None:
    with _shared_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE company_id=?", (company_id,)
        ).fetchone()
    return dict(row) if row else None

def upsert_subscription(s: dict):
    with _shared_conn() as conn:
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
    """Retourne le plan actif pour une boutique ('free' par défaut)."""
    sub = get_subscription(company_id)
    if not sub or sub["status"] not in ("active", "trialing"):
        return "free"
    return sub["plan"]


# ─── Invitations ─────────────────────────────────────────────────────────────

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


# ─── Super-admin global stats ─────────────────────────────────────────────────

def global_stats() -> dict:
    with _shared_conn() as conn:
        n_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE status='active'").fetchone()[0]
        n_users     = conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
    # Agréger les ventes de toutes les boutiques
    total_revenue = 0.0
    total_sales   = 0
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
        "companies": n_companies,
        "users":     n_users,
        "total_sales":   total_sales,
        "total_revenue": total_revenue,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# BASE PAR BOUTIQUE  erp_{company_id}.db
# ═══════════════════════════════════════════════════════════════════════════════

def _tenant_path(company_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_id)
    return cfg.DATA_DIR / f"erp_{safe}.db"


def get_conn(company_id: str) -> sqlite3.Connection:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_tenant_path(company_id)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
            id         TEXT PRIMARY KEY,
            reference  TEXT NOT NULL,
            source     TEXT DEFAULT 'dashboard',
            items      TEXT NOT NULL,
            total      REAL NOT NULL DEFAULT 0,
            customer   TEXT,
            note       TEXT,
            created_at TEXT NOT NULL
        );

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
            id          TEXT PRIMARY KEY,
            code_id     TEXT NOT NULL,
            product_id  TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            latitude    REAL,
            longitude   REAL,
            city        TEXT,
            country     TEXT,
            ip_address  TEXT,
            user_agent  TEXT,
            FOREIGN KEY (code_id) REFERENCES auth_codes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_verif_product ON verifications(product_id);
        CREATE INDEX IF NOT EXISTS idx_verif_code    ON verifications(code_id);
    """)
    return conn


def init_tenant_db(company_id: str):
    """Crée toutes les tables pour une nouvelle boutique."""
    with get_conn(company_id) as conn:
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
                id         TEXT PRIMARY KEY,
                reference  TEXT NOT NULL,
                source     TEXT DEFAULT 'dashboard',
                items      TEXT NOT NULL,
                total      REAL NOT NULL DEFAULT 0,
                customer   TEXT,
                note       TEXT,
                created_at TEXT NOT NULL
            );

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
                id          TEXT PRIMARY KEY,
                code_id     TEXT NOT NULL,
                product_id  TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                latitude    REAL,
                longitude   REAL,
                city        TEXT,
                country     TEXT,
                ip_address  TEXT,
                user_agent  TEXT,
                FOREIGN KEY (code_id) REFERENCES auth_codes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_verif_product ON verifications(product_id);
            CREATE INDEX IF NOT EXISTS idx_verif_code    ON verifications(code_id);
        """)


# ─── Produits ─────────────────────────────────────────────────────────────────

def _blank_product(p: dict) -> dict:
    defaults = {"image_url": None, "reference_image_url": None,
                "reference_image_hash": None, "consumer_code": None}
    return {**defaults, **p}

def all_products(company_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    return [dict(r) for r in rows]

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
        row = conn.execute(
            "SELECT * FROM products WHERE consumer_code=?", (code,)
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


# ─── Ventes ───────────────────────────────────────────────────────────────────

def _sale_row(row) -> dict | None:
    if not row: return None
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d

def all_sales(company_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("SELECT * FROM sales ORDER BY created_at DESC").fetchall()
    return [_sale_row(r) for r in rows]

def count_sales_this_month(company_id: str) -> int:
    from datetime import datetime
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
            INSERT INTO sales (id,reference,source,items,total,customer,note,created_at)
            VALUES (:id,:reference,:source,:items,:total,:customer,:note,:created_at)
        """, {**s, "items": items_json})


# ─── Auth codes ───────────────────────────────────────────────────────────────

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
            SELECT a.*, v.verified_at, v.latitude, v.longitude, v.city, v.country
            FROM auth_codes a
            LEFT JOIN verifications v ON v.code_id = a.id
            WHERE a.product_id=?
            ORDER BY a.created_at DESC
        """, (product_id,)).fetchall()
    return [dict(r) for r in rows]


# ─── Vérifications ────────────────────────────────────────────────────────────

def insert_verification(company_id: str, v: dict):
    with get_conn(company_id) as conn:
        conn.execute("""
            INSERT INTO verifications
                (id,code_id,product_id,verified_at,latitude,longitude,
                 city,country,ip_address,user_agent)
            VALUES
                (:id,:code_id,:product_id,:verified_at,:latitude,:longitude,
                 :city,:country,:ip_address,:user_agent)
        """, v)

def all_verifications(company_id: str) -> list:
    with get_conn(company_id) as conn:
        rows = conn.execute("""
            SELECT v.*, p.name as product_name, a.code
            FROM verifications v
            JOIN products p ON v.product_id = p.id
            JOIN auth_codes a ON v.code_id = a.id
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
    return {
        "total": total,
        "by_product": [dict(r) for r in by_product],
        "by_country": [dict(r) for r in by_country],
    }

def auth_code_aggregate_stats(company_id: str) -> dict:
    with get_conn(company_id) as conn:
        total_codes = conn.execute("SELECT COUNT(*) FROM auth_codes").fetchone()[0]
        used_codes  = conn.execute("SELECT COUNT(*) FROM auth_codes WHERE status='used'").fetchone()[0]
        total_verif = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        fake = conn.execute("""
            SELECT COUNT(*) FROM verifications v
            JOIN auth_codes a ON v.code_id=a.id
            WHERE a.status='used'
              AND v.verified_at != (
                  SELECT MIN(verified_at) FROM verifications v2 WHERE v2.code_id=a.id
              )
        """).fetchone()[0]
    return {"total_codes": total_codes, "used_codes": used_codes,
            "total_verifications": total_verif, "fake_attempts": fake}


# ─── Init ─────────────────────────────────────────────────────────────────────
init_shared_db()
print(f"[DB] Base partagee -> {SHARED_DB}")
