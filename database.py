"""
database.py — v3.2
Tables : products (enrichi), sales, auth_codes, verifications
"""
import sqlite3, json
from pathlib import Path

DB_PATH = Path(__file__).parent / "erp.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_conn() as conn:
        # ── Étape 1 : tables de base (sans index sur les nouvelles colonnes) ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id          TEXT PRIMARY KEY,
                sku         TEXT,
                name        TEXT NOT NULL,
                description TEXT,
                price       REAL NOT NULL,
                stock       INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
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
        """)

        # ── Étape 2 : migrations (ajout des nouvelles colonnes si absentes) ──
        # DOIT tourner AVANT les index qui référencent ces colonnes
        _safe_add_columns(conn, "products", [
            ("company_id",           "TEXT"),
            ("image_url",            "TEXT"),
            ("reference_image_url",  "TEXT"),
            ("reference_image_hash", "TEXT"),
            ("consumer_code",        "TEXT"),
        ])

        # ── Étape 3 : index (maintenant que toutes les colonnes existent) ──
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_products_consumer_code
                ON products(consumer_code);
            CREATE INDEX IF NOT EXISTS idx_products_company
                ON products(company_id);
            CREATE INDEX IF NOT EXISTS idx_auth_codes_product ON auth_codes(product_id);
            CREATE INDEX IF NOT EXISTS idx_auth_codes_code    ON auth_codes(code);
            CREATE INDEX IF NOT EXISTS idx_verif_product ON verifications(product_id);
            CREATE INDEX IF NOT EXISTS idx_verif_code    ON verifications(code_id);
        """)

def _safe_add_columns(conn, table, columns):
    """Ajoute des colonnes seulement si elles n'existent pas encore."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in columns:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except Exception:
                pass

# ─── PRODUITS ────────────────────────────────────────────────────────────────
def all_products() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    return [dict(r) for r in rows]

def get_product(product_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return dict(row) if row else None

def get_product_by_sku(sku: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    return dict(row) if row else None

def get_product_by_consumer_code(code: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE consumer_code=?", (code,)
        ).fetchone()
    return dict(row) if row else None

def insert_product(p: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO products
                (id, sku, name, description, price, stock,
                 company_id, image_url, reference_image_url,
                 reference_image_hash, consumer_code, created_at, updated_at)
            VALUES
                (:id, :sku, :name, :description, :price, :stock,
                 :company_id, :image_url, :reference_image_url,
                 :reference_image_hash, :consumer_code, :created_at, :updated_at)
        """, p)

def update_product_full(p: dict):
    with get_conn() as conn:
        conn.execute("""
            UPDATE products SET
                sku=:sku, name=:name, description=:description,
                price=:price, stock=:stock, company_id=:company_id,
                image_url=:image_url, reference_image_url=:reference_image_url,
                reference_image_hash=:reference_image_hash,
                consumer_code=:consumer_code, updated_at=:updated_at
            WHERE id=:id
        """, p)

def update_product_image(product_id: str, image_url: str,
                         ref_image_url: str | None,
                         ref_image_hash: str | None,
                         updated_at: str):
    with get_conn() as conn:
        conn.execute("""
            UPDATE products SET
                image_url=?, reference_image_url=?,
                reference_image_hash=?, updated_at=?
            WHERE id=?
        """, (image_url, ref_image_url, ref_image_hash, updated_at, product_id))

def update_stock(product_id: str, new_stock: int, updated_at: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE products SET stock=?, updated_at=? WHERE id=?",
            (new_stock, updated_at, product_id)
        )

def delete_product(product_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))

# ─── VENTES ──────────────────────────────────────────────────────────────────
def _sale_row(row) -> dict:
    if not row: return None
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d

def all_sales() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sales ORDER BY created_at DESC").fetchall()
    return [_sale_row(r) for r in rows]

def get_sale(sale_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    return _sale_row(row)

def insert_sale(s: dict):
    items_json = json.dumps(s.get("items", []), ensure_ascii=False)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sales (id,reference,source,items,total,customer,note,created_at)
            VALUES (:id,:reference,:source,:items,:total,:customer,:note,:created_at)
        """, {**s, "items": items_json})

# ─── AUTH CODES ──────────────────────────────────────────────────────────────
def insert_auth_code(c: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO auth_codes (id, product_id, code, status, created_at)
            VALUES (:id, :product_id, :code, :status, :created_at)
        """, c)

def get_auth_code_by_value(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM auth_codes WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None

def mark_code_used(code_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE auth_codes SET status='used' WHERE id=?", (code_id,))

def get_codes_for_product(product_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM auth_codes WHERE product_id=? ORDER BY created_at DESC",
            (product_id,)
        ).fetchall()
    return [dict(r) for r in rows]

# ─── VÉRIFICATIONS ───────────────────────────────────────────────────────────
def insert_verification(v: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO verifications
                (id, code_id, product_id, verified_at,
                 latitude, longitude, city, country, ip_address, user_agent)
            VALUES
                (:id, :code_id, :product_id, :verified_at,
                 :latitude, :longitude, :city, :country, :ip_address, :user_agent)
        """, v)

def get_verifications_for_product(product_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM verifications WHERE product_id=? ORDER BY verified_at DESC",
            (product_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def all_verifications() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT v.*, p.name as product_name
            FROM verifications v
            JOIN products p ON v.product_id = p.id
            ORDER BY v.verified_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

def get_verification_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        by_product = conn.execute("""
            SELECT p.name, p.id, COUNT(v.id) as count
            FROM products p
            LEFT JOIN verifications v ON p.id = v.product_id
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

init_db()
print(f"[DB] SQLite prête → {DB_PATH}")
