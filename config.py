"""
config.py -- Chargement centralise de la configuration depuis .env
"""
import os
from pathlib import Path
from functools import lru_cache

_env_file = Path(__file__).parent / ".env"
_base_dir = Path(__file__).parent
_file_env = {}
if _env_file.exists():
    # Le fichier .env est partage entre Windows et les hebergeurs Linux :
    # utiliser explicitement UTF-8 au lieu de l'encodage local Windows (cp1252).
    for line in _env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            _file_env[key.strip()] = val.strip()


def _get(key, default=""):
    # Le fichier local reste prioritaire afin que les changements soient pris
    # en compte par le reloader Uvicorn, dont le processus parent peut conserver
    # d'anciennes variables d'environnement en mémoire.
    return _file_env.get(key, os.environ.get(key, default))

def _get_int(key, default):
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default

def _get_float(key, default):
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default

def _get_list(key, default=""):
    val = _get(key, default)
    return [v.strip() for v in val.split(",") if v.strip()]

def _get_path(key, default):
    raw = _get(key, default).strip()
    path = Path(raw)
    if path.is_absolute():
        return path
    return (_base_dir / path).resolve()


@lru_cache(maxsize=1)
def get_settings():
    return Settings()


class Settings:
    # Les comptes de démonstration sont désactivés par défaut, y compris en
    # production. Pour un environnement local, définissez explicitement
    # ENABLE_DEMO_DATA=true.
    APP_ENV = _get("APP_ENV", "production").strip().lower()
    ENABLE_DEMO_DATA = _get("ENABLE_DEMO_DATA", "false").lower() in ("1", "true", "yes", "on")

    # JWT
    JWT_SECRET = _get("JWT_SECRET", "dev-secret-CHANGE-IN-PROD-min32chars!!")
    JWT_ALGORITHM = _get("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES = _get_int("ACCESS_TOKEN_EXPIRE_MINUTES", 60)
    REFRESH_TOKEN_EXPIRE_DAYS = _get_int("REFRESH_TOKEN_EXPIRE_DAYS", 30)
    INVITE_TOKEN_EXPIRE_HOURS = _get_int("INVITE_TOKEN_EXPIRE_HOURS", 48)
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS = _get_int("EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS", 48)
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = _get_int("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", 30)

    # Super-admin
    SUPERADMIN_EMAIL = _get("SUPERADMIN_EMAIL", "admin@tpe-qr.com")
    SUPERADMIN_PASSWORD = _get("SUPERADMIN_PASSWORD", "AdminPassword123!")

    # CORS
    ALLOWED_ORIGINS = _get_list(
        "ALLOWED_ORIGINS",
        "http://localhost:8000,http://localhost:3000,http://127.0.0.1:8000"
    )

    # Rate limiting
    RATE_LIMIT_VERIFY_REQUESTS = _get_int("RATE_LIMIT_VERIFY_REQUESTS", 10)
    RATE_LIMIT_VERIFY_WINDOW   = _get_int("RATE_LIMIT_VERIFY_WINDOW", 60)
    RATE_LIMIT_LOGIN_REQUESTS  = _get_int("RATE_LIMIT_LOGIN_REQUESTS", 5)
    RATE_LIMIT_LOGIN_WINDOW    = _get_int("RATE_LIMIT_LOGIN_WINDOW", 60)

    # Plans et quotas
    PLAN_LIMITS = {
        "free": {
            "products": _get_int("PLAN_FREE_PRODUCTS", 10),
            "users":    _get_int("PLAN_FREE_USERS", 1),
            "tx_month": _get_int("PLAN_FREE_TRANSACTIONS", 100),
        },
        "basic": {
            "products": _get_int("PLAN_BASIC_PRODUCTS", 100),
            "users":    _get_int("PLAN_BASIC_USERS", 3),
            "tx_month": _get_int("PLAN_BASIC_TRANSACTIONS", 1000),
        },
        "pro": {
            "products": _get_int("PLAN_PRO_PRODUCTS", 1000),
            "users":    _get_int("PLAN_PRO_USERS", 10),
            "tx_month": _get_int("PLAN_PRO_TRANSACTIONS", 10000),
        },
        "enterprise": {
            "products": _get_int("PLAN_ENTERPRISE_PRODUCTS", 999999),
            "users":    _get_int("PLAN_ENTERPRISE_USERS", 999999),
            "tx_month": _get_int("PLAN_ENTERPRISE_TRANSACTIONS", 999999),
        },
    }

    # Chemins
    DATA_DIR   = _get_path("DATA_DIR", "./data")
    BACKUP_DIR = _get_path("BACKUP_DIR", "./backups")

    # Base de donnees : sqlite | mysql
    DB_ENGINE      = _get("DB_ENGINE", "sqlite").lower()
    MYSQL_HOST     = _get("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT     = _get_int("MYSQL_PORT", 3306)
    MYSQL_DATABASE = _get("MYSQL_DATABASE", "quicksellpay")
    MYSQL_USER     = _get("MYSQL_USER", "quicksellpay")
    MYSQL_PASSWORD = _get("MYSQL_PASSWORD", "")
    MYSQL_POOL_SIZE = _get_int("MYSQL_POOL_SIZE", 10)
    MYSQL_POOL_TIMEOUT = _get_int("MYSQL_POOL_TIMEOUT", 10)
    MYSQL_CONNECT_TIMEOUT = _get_int("MYSQL_CONNECT_TIMEOUT", 10)

    # Stockage images : local | s3 | r2
    STORAGE_PROVIDER          = _get("STORAGE_PROVIDER", "local").lower()
    STORAGE_BUCKET            = _get("STORAGE_BUCKET", "")
    STORAGE_REGION            = _get("STORAGE_REGION", "auto")
    STORAGE_ENDPOINT_URL      = _get("STORAGE_ENDPOINT_URL", "")
    STORAGE_ACCESS_KEY_ID     = _get("STORAGE_ACCESS_KEY_ID", "")
    STORAGE_SECRET_ACCESS_KEY = _get("STORAGE_SECRET_ACCESS_KEY", "")
    STORAGE_PUBLIC_BASE_URL   = _get("STORAGE_PUBLIC_BASE_URL", "")
    MAX_UPLOAD_SIZE_MB        = _get_int("MAX_UPLOAD_SIZE_MB", 10)

    # Email SMTP
    MAIL_ENABLED        = _get("MAIL_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    MAIL_HOST           = _get("MAIL_HOST", "")
    MAIL_PORT           = _get_int("MAIL_PORT", 587)
    MAIL_USERNAME       = _get("MAIL_USERNAME", "")
    MAIL_PASSWORD       = _get("MAIL_PASSWORD", "")
    MAIL_FROM           = _get("MAIL_FROM", "no-reply@tpe-qr.local")
    MAIL_USE_TLS        = _get("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes", "on")
    PUBLIC_APP_BASE_URL = _get("PUBLIC_APP_BASE_URL", "http://localhost:8000")

    # Redis -- rate limiting multi-workers
    # Format : redis://:password@host:6379/0
    # Si vide, fallback in-memory (ok pour 1 seul worker uvicorn)
    REDIS_URL = _get("REDIS_URL", "")

    # Sentry -- monitoring des erreurs en production
    # Creer un projet gratuit sur https://sentry.io
    SENTRY_DSN                = _get("SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE = _get_float("SENTRY_TRACES_SAMPLE_RATE", 0.05)

    # FedaPay -- passerelle paiement West Africa (MTN MoMo, Moov, cartes)
    # Creer un compte gratuit sur https://fedapay.com
    # FEDAPAY_ENV = sandbox (tests) ou live (production)
    FEDAPAY_SECRET_KEY      = _get("FEDAPAY_SECRET_KEY", "")
    FEDAPAY_WEBHOOK_SECRET  = _get("FEDAPAY_WEBHOOK_SECRET", "")
    FEDAPAY_ENV             = _get("FEDAPAY_ENV", "sandbox")
    # Tarifs mensuels en XOF (Franc CFA d'Afrique de l'Ouest)
    FEDAPAY_PRICE_BASIC      = _get_int("FEDAPAY_PRICE_BASIC",      5000)
    FEDAPAY_PRICE_PRO        = _get_int("FEDAPAY_PRICE_PRO",       10000)
    FEDAPAY_PRICE_ENTERPRISE = _get_int("FEDAPAY_PRICE_ENTERPRISE", 15000)

    def validate_security(self) -> None:
        """Refuse un démarrage production avec les secrets de développement."""
        if self.APP_ENV != "production":
            return
        insecure = []
        if self.JWT_SECRET == "dev-secret-CHANGE-IN-PROD-min32chars!!" or len(self.JWT_SECRET) < 32:
            insecure.append("JWT_SECRET")
        if self.SUPERADMIN_PASSWORD == "AdminPassword123!" or len(self.SUPERADMIN_PASSWORD) < 12:
            insecure.append("SUPERADMIN_PASSWORD")
        if insecure:
            raise RuntimeError(
                "Configuration production non sécurisée : configurez "
                + ", ".join(insecure)
                + " dans les variables d'environnement."
            )
