import sqlite3
import json
from pathlib import Path

# ─── Chemin du fichier base de données ────────────────────────────────────────
DB_PATH = Path(__file__).parent / "erp.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Crée les tables si elles n'existent pas encore."""
    with get_conn() as conn:
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
                id          TEXT PRIMARY KEY,
                reference   TEXT NOT NULL,
                source      TEXT DEFAULT 'dashboard',
                items       TEXT NOT NULL,
                total       REAL NOT NULL DEFAULT 0,
                customer    TEXT,
                note        TEXT,
                created_at  TEXT NOT NULL
            );
        """)

# ─── Helpers produits ─────────────────────────────────────────────────────────
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

def insert_product(p: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO products (id,sku,name,description,price,stock,created_at,updated_at)
            VALUES (:id,:sku,:name,:description,:price,:stock,:created_at,:updated_at)
        """, p)

def update_product_full(p: dict):
    with get_conn() as conn:
        conn.execute("""
            UPDATE products SET sku=:sku,name=:name,description=:description,
            price=:price,stock=:stock,updated_at=:updated_at WHERE id=:id
        """, p)

def update_stock(product_id: str, new_stock: int, updated_at: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE products SET stock=?,updated_at=? WHERE id=?",
            (new_stock, updated_at, product_id)
        )

def delete_product(product_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))

# ─── Helpers ventes ───────────────────────────────────────────────────────────
def _sale_row(row) -> dict:
    if not row:
        return None
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

# ─── Init au démarrage ────────────────────────────────────────────────────────
init_db()
print(f"[DB] SQLite prête -> {DB_PATH}")
