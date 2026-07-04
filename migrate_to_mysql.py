"""
migrate_to_mysql.py — Migration SQLite → MySQL pour QuickSellPay v5

Usage :
    # 1. Configurer .env avec DB_ENGINE=mysql et les credentials MySQL
    # 2. Lancer la migration
    python migrate_to_mysql.py

    # Optionnel : ne migrer qu'une boutique spécifique
    python migrate_to_mysql.py --company demo-shop

Ce script :
  1. Lit la base partagée SQLite (shared.db) → companies, users, subscriptions, tokens
  2. Lit chaque base boutique SQLite (erp_*.db) → products, sales, auth_codes, verifications
  3. Insère tout dans MySQL (idempotent via INSERT IGNORE / ON DUPLICATE KEY)
  4. Affiche un rapport final

Pré-requis :
  - pip install PyMySQL
  - Base MySQL vide ou existante (les tables sont créées automatiquement)
  - .env configuré avec DB_ENGINE=mysql
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# ── Bootstrap ──────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from config import get_settings
from db_driver import mysql_conn, mysql_enabled, MYSQL_SHARED_SCHEMA, MYSQL_TENANT_SCHEMA

cfg = get_settings()

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sqlite_rows(db_path: Path, query: str, params=()) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [WARN] SQLite query failed on {db_path.name}: {e}")
        return []
    finally:
        conn.close()


def _mysql_insert_ignore(mysql, table: str, rows: list[dict], pk: str = "id") -> int:
    if not rows:
        return 0
    inserted = 0
    for row in rows:
        cols   = list(row.keys())
        values = list(row.values())
        placeholders = ", ".join(["%s"] * len(cols))
        col_names    = ", ".join(f"`{c}`" for c in cols)
        sql = f"INSERT IGNORE INTO `{table}` ({col_names}) VALUES ({placeholders})"
        try:
            mysql.execute(sql, values)
            inserted += 1
        except Exception as e:
            print(f"  [WARN] {table} INSERT failed for {row.get(pk, '?')}: {e}")
    return inserted


def _progress(label: str, n: int):
    print(f"  ✓ {label}: {n} lignes migrées")


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION BASE PARTAGÉE
# ══════════════════════════════════════════════════════════════════════════════

def migrate_shared(mysql, only_company: str | None = None):
    print("\n── Base partagée (companies / users / subscriptions / tokens) ──")
    shared_db = cfg.DATA_DIR / "shared.db"

    # ── Companies ─────────────────────────────────────────────────────────────
    companies = _sqlite_rows(shared_db, "SELECT * FROM companies")
    if only_company:
        companies = [c for c in companies if c["id"] == only_company]
    n = _mysql_insert_ignore(mysql, "companies", companies)
    _progress("companies", n)

    # ── Users ─────────────────────────────────────────────────────────────────
    if only_company:
        users = _sqlite_rows(shared_db, "SELECT * FROM users WHERE company_id=?", (only_company,))
    else:
        users = _sqlite_rows(shared_db, "SELECT * FROM users")
    # Ajouter revoked_before si absent (ancienne colonne)
    for u in users:
        u.setdefault("revoked_before", None)
    n = _mysql_insert_ignore(mysql, "users", users)
    _progress("users", n)

    # ── Subscriptions ─────────────────────────────────────────────────────────
    if only_company:
        subs = _sqlite_rows(shared_db, "SELECT * FROM subscriptions WHERE company_id=?", (only_company,))
    else:
        subs = _sqlite_rows(shared_db, "SELECT * FROM subscriptions")
    n = _mysql_insert_ignore(mysql, "subscriptions", subs)
    _progress("subscriptions", n)

    # ── Invite tokens ─────────────────────────────────────────────────────────
    if only_company:
        invites = _sqlite_rows(shared_db, "SELECT * FROM invite_tokens WHERE company_id=?", (only_company,))
    else:
        invites = _sqlite_rows(shared_db, "SELECT * FROM invite_tokens")
    n = _mysql_insert_ignore(mysql, "invite_tokens", invites, pk="token")
    _progress("invite_tokens", n)

    # ── Email verification tokens ─────────────────────────────────────────────
    ev_tokens = _sqlite_rows(shared_db, "SELECT * FROM email_verification_tokens")
    n = _mysql_insert_ignore(mysql, "email_verification_tokens", ev_tokens, pk="token")
    _progress("email_verification_tokens", n)

    # ── Password reset tokens ─────────────────────────────────────────────────
    pr_tokens = _sqlite_rows(shared_db, "SELECT * FROM password_reset_tokens")
    n = _mysql_insert_ignore(mysql, "password_reset_tokens", pr_tokens, pk="token")
    _progress("password_reset_tokens", n)


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION D'UNE BOUTIQUE
# ══════════════════════════════════════════════════════════════════════════════

def migrate_tenant(mysql, company_id: str):
    safe    = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_id)
    db_path = cfg.DATA_DIR / f"erp_{safe}.db"

    if not db_path.exists():
        print(f"  [SKIP] Pas de base SQLite pour {company_id}")
        return

    print(f"\n── Boutique : {company_id} ──")

    # ── Products ──────────────────────────────────────────────────────────────
    products = _sqlite_rows(db_path, "SELECT * FROM products")
    # Ajouter company_id si absent (ancienne schema sans company_id dans SQLite tenant)
    for p in products:
        p["company_id"] = company_id
    n = _mysql_insert_ignore(mysql, "products", products)
    _progress("products", n)

    # ── Sales ─────────────────────────────────────────────────────────────────
    sales = _sqlite_rows(db_path, "SELECT * FROM sales")
    for s in sales:
        s["company_id"] = company_id
        # Assurer que items est une string JSON
        if isinstance(s.get("items"), list):
            s["items"] = json.dumps(s["items"], ensure_ascii=False)
    n = _mysql_insert_ignore(mysql, "sales", sales)
    _progress("sales", n)

    # ── Auth codes ────────────────────────────────────────────────────────────
    codes = _sqlite_rows(db_path, "SELECT * FROM auth_codes")
    for c in codes:
        c["company_id"] = company_id
    n = _mysql_insert_ignore(mysql, "auth_codes", codes)
    _progress("auth_codes", n)

    # ── Verifications ─────────────────────────────────────────────────────────
    verifs = _sqlite_rows(db_path, "SELECT * FROM verifications")
    for v in verifs:
        v["company_id"] = company_id
    n = _mysql_insert_ignore(mysql, "verifications", verifs)
    _progress("verifications", n)

    # ── Audit logs ────────────────────────────────────────────────────────────
    try:
        logs = _sqlite_rows(db_path, "SELECT * FROM audit_logs")
        for l in logs:
            l["company_id"] = company_id
            if isinstance(l.get("details"), dict):
                l["details"] = json.dumps(l["details"], ensure_ascii=False)
        n = _mysql_insert_ignore(mysql, "audit_logs", logs)
        _progress("audit_logs", n)
    except Exception as e:
        print(f"  [WARN] audit_logs ignorés : {e}")


# ══════════════════════════════════════════════════════════════════════════════
# INIT SCHÉMA MYSQL
# ══════════════════════════════════════════════════════════════════════════════

def init_mysql_schema(mysql):
    print("\n── Création du schéma MySQL ──")
    # Schéma partagé
    for stmt in MYSQL_SHARED_SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            try:
                mysql.execute(s)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    print(f"  [WARN] DDL: {e}")
    # Schéma tenant (tables avec company_id)
    for stmt in MYSQL_TENANT_SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            try:
                mysql.execute(s)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    print(f"  [WARN] DDL: {e}")
    print("  ✓ Schéma OK")


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Migration SQLite → MySQL pour QuickSellPay")
    parser.add_argument("--company", help="Migrer uniquement cette boutique (company_id)", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Simuler sans écrire dans MySQL")
    args = parser.parse_args()

    if not mysql_enabled():
        print("ERREUR : DB_ENGINE n'est pas 'mysql' dans votre .env")
        print("Mettez DB_ENGINE=mysql et configurez MYSQL_HOST, MYSQL_DATABASE, etc.")
        sys.exit(1)

    print(f"Migration SQLite → MySQL : {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}/{cfg.MYSQL_DATABASE}")
    if args.dry_run:
        print("⚠ MODE DRY-RUN : aucune donnée ne sera écrite")
        return

    try:
        # Une seule connexion MySQL sans scoping tenant pour tout le script
        with mysql_conn(None) as mysql:
            init_mysql_schema(mysql)
            migrate_shared(mysql, only_company=args.company)

            # Lister les boutiques à migrer
            if args.company:
                company_ids = [args.company]
            else:
                shared_db = cfg.DATA_DIR / "shared.db"
                if shared_db.exists():
                    conn = sqlite3.connect(str(shared_db))
                    rows = conn.execute("SELECT id FROM companies").fetchall()
                    company_ids = [r[0] for r in rows]
                    conn.close()
                else:
                    company_ids = [
                        f.stem[4:]
                        for f in cfg.DATA_DIR.glob("erp_*.db")
                    ]

            print(f"\n{len(company_ids)} boutique(s) à migrer...")
            for cid in company_ids:
                migrate_tenant(mysql, cid)

        print("\n══════════════════════════════════════")
        print("  Migration terminée avec succès ✓")
        print("  Prochaine étape : changer DB_ENGINE=mysql dans .env")
        print("  puis redémarrer le serveur : uvicorn main:app --reload")
        print("══════════════════════════════════════")

    except Exception as exc:
        print(f"\nERREUR FATALE : {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
