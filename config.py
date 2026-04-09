"""
config.py — Chargement centralisé de la configuration depuis .env
"""
import os
from pathlib import Path
from functools import lru_cache

# Charger .env si présent (sans dépendance python-dotenv obligatoire)
_env_file = Path(__file__).parent / ".env"
_base_dir = Path(__file__).parent
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default

def _get_list(key: str, default: str = "") -> list[str]:
    val = os.environ.get(key, default)
    return [v.strip() for v in val.split(",") if v.strip()]

def _get_path(key: str, default: str) -> Path:
    raw = os.environ.get(key, default).strip()
    path = Path(raw)
    if path.is_absolute():
        return path
    return (_base_dir / path).resolve()


@lru_cache(maxsize=1)
def get_settings():
    return Settings()


class Settings:
    # JWT
    JWT_SECRET: str = _get("JWT_SECRET", "dev-secret-CHANGE-IN-PROD-min32chars!!")
    JWT_ALGORITHM: str = _get("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = _get_int("ACCESS_TOKEN_EXPIRE_MINUTES", 60)
    REFRESH_TOKEN_EXPIRE_DAYS: int = _get_int("REFRESH_TOKEN_EXPIRE_DAYS", 30)
    INVITE_TOKEN_EXPIRE_HOURS: int = _get_int("INVITE_TOKEN_EXPIRE_HOURS", 48)

    # Super-admin
    SUPERADMIN_EMAIL: str = _get("SUPERADMIN_EMAIL", "admin@tpe-qr.com")
    SUPERADMIN_PASSWORD: str = _get("SUPERADMIN_PASSWORD", "AdminPassword123!")

    # CORS
    ALLOWED_ORIGINS: list[str] = _get_list(
        "ALLOWED_ORIGINS",
        "http://localhost:8000,http://localhost:3000,http://127.0.0.1:8000"
    )

    # Rate limiting
    RATE_LIMIT_VERIFY_REQUESTS: int = _get_int("RATE_LIMIT_VERIFY_REQUESTS", 10)
    RATE_LIMIT_VERIFY_WINDOW: int = _get_int("RATE_LIMIT_VERIFY_WINDOW", 60)
    RATE_LIMIT_LOGIN_REQUESTS: int = _get_int("RATE_LIMIT_LOGIN_REQUESTS", 5)
    RATE_LIMIT_LOGIN_WINDOW: int = _get_int("RATE_LIMIT_LOGIN_WINDOW", 60)

    # Stripe
    STRIPE_SECRET_KEY: str = _get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = _get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICES: dict[str, str] = {
        "basic":      _get("STRIPE_PRICE_BASIC", ""),
        "pro":        _get("STRIPE_PRICE_PRO", ""),
        "enterprise": _get("STRIPE_PRICE_ENTERPRISE", ""),
    }

    # Limites par plan : (max_products, max_users, max_transactions_per_month)
    PLAN_LIMITS: dict[str, dict] = {
        "free":       {"products": _get_int("PLAN_FREE_PRODUCTS", 10),
                       "users":    _get_int("PLAN_FREE_USERS", 1),
                       "tx_month": _get_int("PLAN_FREE_TRANSACTIONS", 100)},
        "basic":      {"products": _get_int("PLAN_BASIC_PRODUCTS", 100),
                       "users":    _get_int("PLAN_BASIC_USERS", 3),
                       "tx_month": _get_int("PLAN_BASIC_TRANSACTIONS", 1000)},
        "pro":        {"products": _get_int("PLAN_PRO_PRODUCTS", 1000),
                       "users":    _get_int("PLAN_PRO_USERS", 10),
                       "tx_month": _get_int("PLAN_PRO_TRANSACTIONS", 10000)},
        "enterprise": {"products": _get_int("PLAN_ENTERPRISE_PRODUCTS", 999999),
                       "users":    _get_int("PLAN_ENTERPRISE_USERS", 999999),
                       "tx_month": _get_int("PLAN_ENTERPRISE_TRANSACTIONS", 999999)},
    }

    # Stockage
    DATA_DIR: Path = _get_path("DATA_DIR", "./data")
    BACKUP_DIR: Path = _get_path("BACKUP_DIR", "./backups")

    # Stockage images: local | s3 | r2
    STORAGE_PROVIDER: str = _get("STORAGE_PROVIDER", "local").lower()
    STORAGE_BUCKET: str = _get("STORAGE_BUCKET", "")
    STORAGE_REGION: str = _get("STORAGE_REGION", "auto")
    STORAGE_ENDPOINT_URL: str = _get("STORAGE_ENDPOINT_URL", "")
    STORAGE_ACCESS_KEY_ID: str = _get("STORAGE_ACCESS_KEY_ID", "")
    STORAGE_SECRET_ACCESS_KEY: str = _get("STORAGE_SECRET_ACCESS_KEY", "")
    STORAGE_PUBLIC_BASE_URL: str = _get("STORAGE_PUBLIC_BASE_URL", "")
