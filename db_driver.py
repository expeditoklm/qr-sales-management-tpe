import re
from typing import Any, Iterable

from config import get_settings


cfg = get_settings()
TENANT_TABLES = ("products", "sales", "audit_logs", "auth_codes", "verifications")


MYSQL_SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id VARCHAR(191) PRIMARY KEY,
    name TEXT NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    secret_key VARCHAR(255) NOT NULL DEFAULT '',
    logo_url TEXT,
    commercial_name TEXT,
    rccm TEXT,
    ifu TEXT,
    address TEXT,
    phone TEXT,
    contact_email TEXT,
    is_vat_registered INTEGER NOT NULL DEFAULT 1,
    mecef_token TEXT,
    low_stock_threshold INTEGER NOT NULL DEFAULT 10,
    plan VARCHAR(64) NOT NULL DEFAULT 'free',
    status VARCHAR(64) NOT NULL DEFAULT 'active',
    created_at VARCHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role VARCHAR(64) NOT NULL DEFAULT 'employee',
    is_active INTEGER NOT NULL DEFAULT 1,
    email_verified INTEGER NOT NULL DEFAULT 0,
    revoked_before VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE TABLE IF NOT EXISTS subscriptions (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL UNIQUE,
    plan VARCHAR(64) NOT NULL DEFAULT 'free',
    status VARCHAR(64) NOT NULL DEFAULT 'active',
    start_date VARCHAR(64) NOT NULL,
    end_date VARCHAR(64),
    stripe_subscription_id TEXT,
    stripe_customer_id TEXT,
    updated_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_sub_company ON subscriptions(company_id);
CREATE TABLE IF NOT EXISTS invite_tokens (
    token VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    email VARCHAR(255) NOT NULL,
    role VARCHAR(64) NOT NULL DEFAULT 'employee',
    expires_at VARCHAR(64) NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token VARCHAR(191) PRIMARY KEY,
    user_id VARCHAR(191) NOT NULL,
    email VARCHAR(255) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token VARCHAR(191) PRIMARY KEY,
    user_id VARCHAR(191) NOT NULL,
    email VARCHAR(255) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS token_blacklist (
    jti        VARCHAR(191) PRIMARY KEY,
    user_id    VARCHAR(191) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_bl_expires (expires_at),
    INDEX idx_bl_user    (user_id)
);
"""


MYSQL_TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    sku VARCHAR(191),
    name TEXT NOT NULL,
    description TEXT,
    price DOUBLE NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    image_url TEXT,
    reference_image_url TEXT,
    reference_image_hash TEXT,
    consumer_code VARCHAR(191),
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    INDEX idx_products_company (company_id)
);
CREATE INDEX IF NOT EXISTS idx_prod_consumer ON products(consumer_code);
CREATE INDEX IF NOT EXISTS idx_prod_sku ON products(sku);
CREATE TABLE IF NOT EXISTS sales (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    reference TEXT NOT NULL,
    source VARCHAR(64) DEFAULT 'dashboard',
    items TEXT NOT NULL,
    total DOUBLE NOT NULL DEFAULT 0,
    customer TEXT,
    note TEXT,
    created_by_user_id VARCHAR(191),
    created_by_email VARCHAR(255),
    created_by_role VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_sales_company (company_id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    user_id VARCHAR(191),
    user_email VARCHAR(255),
    user_role VARCHAR(64),
    action TEXT NOT NULL,
    object_type VARCHAR(128) NOT NULL,
    object_id VARCHAR(191),
    object_label TEXT,
    details TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_audit_company (company_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_logs(object_type, object_id);
CREATE TABLE IF NOT EXISTS auth_codes (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    product_id VARCHAR(191) NOT NULL,
    code VARCHAR(191) NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'active',
    created_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    UNIQUE KEY uq_codes_company_code (company_id, code),
    INDEX idx_codes_company (company_id)
);
CREATE INDEX IF NOT EXISTS idx_codes_product ON auth_codes(product_id);
CREATE INDEX IF NOT EXISTS idx_codes_code ON auth_codes(code);
CREATE TABLE IF NOT EXISTS verifications (
    id VARCHAR(191) PRIMARY KEY,
    company_id VARCHAR(191) NOT NULL,
    code_id VARCHAR(191),
    product_id VARCHAR(191) NOT NULL,
    verified_at VARCHAR(64) NOT NULL,
    latitude DOUBLE,
    longitude DOUBLE,
    city TEXT,
    country TEXT,
    ip_address TEXT,
    user_agent TEXT,
    code_value VARCHAR(191),
    attempt_type VARCHAR(64) NOT NULL DEFAULT 'valid',
    is_valid INTEGER NOT NULL DEFAULT 1,
    is_fraud INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    FOREIGN KEY (code_id) REFERENCES auth_codes(id),
    INDEX idx_verif_company (company_id)
);
CREATE INDEX IF NOT EXISTS idx_verif_product ON verifications(product_id);
CREATE INDEX IF NOT EXISTS idx_verif_code ON verifications(code_id);
"""


class MySQLResult:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return MySQLRow(row) if row is not None else None

    def fetchall(self):
        return [MySQLRow(row) for row in self.cursor.fetchall()]


class MySQLRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class MySQLConnection:
    def __init__(self, tenant_id: str | None = None):
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "PyMySQL n'est pas installe. Lancez `pip install -r requirements.txt`."
            ) from exc

        self.tenant_id = tenant_id
        self._pymysql = pymysql
        self._conn = pymysql.connect(
            host=cfg.MYSQL_HOST,
            port=cfg.MYSQL_PORT,
            user=cfg.MYSQL_USER,
            password=cfg.MYSQL_PASSWORD,
            database=cfg.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )
        self._conn.cursor().execute("SET FOREIGN_KEY_CHECKS=1")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self.close()

    def close(self):
        self._conn.close()

    def execute(self, sql: str, params: Any = None):
        cursor = self._conn.cursor()
        prepared = self._prepare_sql(sql)
        if isinstance(prepared, tuple):
            sql, params = prepared
        else:
            sql = prepared
            sql, params = self._scope_tenant_sql(sql, params)
        try:
            cursor.execute(sql, params)
        except self._pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] in (1060, 1061):
                return MySQLResult(cursor)
            raise
        except self._pymysql.err.InternalError as exc:
            if exc.args and exc.args[0] in (1060, 1061):
                return MySQLResult(cursor)
            raise
        return MySQLResult(cursor)

    def executescript(self, script: str):
        if "CREATE TABLE IF NOT EXISTS products" in script:
            script = MYSQL_TENANT_SCHEMA
        elif "CREATE TABLE IF NOT EXISTS companies" in script:
            script = MYSQL_SHARED_SCHEMA
        for statement in _split_sql(script):
            if statement:
                self.execute(statement)

    def _prepare_sql(self, sql: str) -> str:
        stripped = sql.strip()
        if stripped.upper().startswith("PRAGMA TABLE_INFO"):
            table = stripped[stripped.find("(") + 1 : stripped.rfind(")")].strip()
            return (
                "SELECT COLUMN_NAME AS name FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
            ), (table,)
        sql = re.sub(r"\?", "%s", sql)
        sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql)
        sql = re.sub(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS", "CREATE INDEX", sql, flags=re.I)
        return sql

    def _scope_tenant_sql(self, sql: str, params: Any) -> tuple[str, Any]:
        if not self.tenant_id:
            return sql, params
        table_scope = _tenant_table_in_sql(sql)
        if not table_scope:
            return sql, params
        table, scope_name = table_scope

        if re.search(rf"INSERT\s+INTO\s+{table}\s*\(", sql, flags=re.I):
            columns_start = sql.find("(")
            values_match = re.search(r"\bVALUES\s*\(", sql, flags=re.I)
            if values_match and "company_id" not in sql[columns_start:values_match.start()].lower():
                sql = sql[: columns_start + 1] + "company_id," + sql[columns_start + 1 :]
                values_match = re.search(r"\bVALUES\s*\(", sql, flags=re.I)
                values_pos = values_match.end()
                sql = sql[:values_pos] + "%(company_id)s," + sql[values_pos:]
            if isinstance(params, dict):
                params = {**params, "company_id": self.tenant_id}
            return sql, params

        if "company_id" in sql.lower():
            return sql, params

        if isinstance(params, dict):
            clause = f"{scope_name}.company_id=%(company_id)s"
            params = {**params, "company_id": self.tenant_id}
        else:
            clause = f"{scope_name}.company_id=%s"
        if re.match(r"\s*(SELECT|UPDATE|DELETE)\b", sql, flags=re.I):
            if isinstance(params, dict):
                sql = _add_where_scope(sql, clause, first=True)
            else:
                tail_placeholders = _tail_placeholder_count(sql)
                sql = _add_where_scope(sql, clause, first=False)
                params = _insert_param_before_tail(params, self.tenant_id, tail_placeholders)
        return sql, params


def _split_sql(script: str) -> Iterable[str]:
    for statement in script.split(";"):
        cleaned = statement.strip()
        if cleaned:
            yield cleaned


def _tenant_table_in_sql(sql: str) -> tuple[str, str] | None:
    for table in TENANT_TABLES:
        match = re.search(
            rf"\b(FROM|UPDATE|INTO)\s+{table}\b(?:\s+([A-Za-z_][A-Za-z0-9_]*))?",
            sql,
            flags=re.I,
        )
        if match:
            alias = match.group(2)
            if alias and alias.upper() not in {"WHERE", "SET", "VALUES", "ORDER", "LEFT", "RIGHT", "INNER", "JOIN"}:
                return table, alias
            return table, table
    return None


def _add_where_scope(sql: str, clause: str, first: bool = True) -> str:
    order_match = re.search(r"\sORDER\s+BY\s", sql, flags=re.I)
    limit_match = re.search(r"\sLIMIT\s", sql, flags=re.I)
    group_match = re.search(r"\sGROUP\s+BY\s", sql, flags=re.I)
    having_match = re.search(r"\sHAVING\s", sql, flags=re.I)
    positions = [m.start() for m in (group_match, having_match, order_match, limit_match) if m]
    split_at = min(positions) if positions else None
    head = sql[:split_at] if split_at is not None else sql
    tail = sql[split_at:] if split_at is not None else ""
    if re.search(r"\bWHERE\b", head, flags=re.I):
        if first:
            return re.sub(r"\bWHERE\b", f"WHERE {clause} AND", head, count=1, flags=re.I) + tail
        return f"{head} AND {clause}{tail}"
    return f"{head} WHERE {clause}{tail}"


def _append_param(params: Any, value: Any):
    if params is None:
        return (value,)
    if isinstance(params, tuple):
        return (*params, value)
    if isinstance(params, list):
        return [*params, value]
    return params


def _insert_param_before_tail(params: Any, value: Any, tail_placeholders: int):
    if params is None:
        return (value,)
    if isinstance(params, tuple):
        index = max(0, len(params) - tail_placeholders)
        return (*params[:index], value, *params[index:])
    if isinstance(params, list):
        index = max(0, len(params) - tail_placeholders)
        return [*params[:index], value, *params[index:]]
    return params


def _tail_placeholder_count(sql: str) -> int:
    order_match = re.search(r"\sORDER\s+BY\s", sql, flags=re.I)
    limit_match = re.search(r"\sLIMIT\s", sql, flags=re.I)
    group_match = re.search(r"\sGROUP\s+BY\s", sql, flags=re.I)
    having_match = re.search(r"\sHAVING\s", sql, flags=re.I)
    positions = [m.start() for m in (group_match, having_match, order_match, limit_match) if m]
    if not positions:
        return 0
    tail = sql[min(positions):]
    return tail.count("%s")


def mysql_enabled() -> bool:
    return cfg.DB_ENGINE == "mysql"


def mysql_conn(tenant_id: str | None = None) -> MySQLConnection:
    return MySQLConnection(tenant_id=tenant_id)
