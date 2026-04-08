"""
auth.py — v4.0
JWT Bearer + bcrypt + dépendances FastAPI pour protection des routes
"""
import uuid, secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import jwt
import bcrypt

import database as db
from config import get_settings

cfg = get_settings()

# ─── Schéma Bearer ────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════════════════════════════════════
# HASHAGE MOT DE PASSE
# ═══════════════════════════════════════════════════════════════════════════════

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# TOKENS JWT
# ═══════════════════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def create_access_token(user_id: str, company_id: str, role: str) -> str:
    payload = {
        "sub":        user_id,
        "company_id": company_id,
        "role":       role,
        "type":       "access",
        "exp":        _now_utc() + timedelta(minutes=cfg.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat":        _now_utc(),
        "jti":        str(uuid.uuid4()),
    }
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def create_refresh_token(user_id: str, company_id: str, role: str) -> str:
    payload = {
        "sub":        user_id,
        "company_id": company_id,
        "role":       role,
        "type":       "refresh",
        "exp":        _now_utc() + timedelta(days=cfg.REFRESH_TOKEN_EXPIRE_DAYS),
        "iat":        _now_utc(),
        "jti":        str(uuid.uuid4()),
    }
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def create_invite_token(company_id: str, email: str, role: str) -> str:
    """Crée un token d'invitation signé (opaque)."""
    token = secrets.token_urlsafe(32)
    expires = (_now_utc() + timedelta(hours=cfg.INVITE_TOKEN_EXPIRE_HOURS)).isoformat()
    db.insert_invite({
        "token":      token,
        "company_id": company_id,
        "email":      email.lower(),
        "role":       role,
        "expires_at": expires,
        "used":       0,
    })
    return token


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expiré")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalide")


# ═══════════════════════════════════════════════════════════════════════════════
# DÉPENDANCES FastAPI
# ═══════════════════════════════════════════════════════════════════════════════

class TokenData:
    def __init__(self, payload: dict):
        self.user_id:    str = payload["sub"]
        self.company_id: str = payload["company_id"]
        self.role:       str = payload.get("role", "employee")

    @property
    def is_admin(self) -> bool:
        return self.role in ("manager", "admin", "superadmin")

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


def _extract_bearer(creds: HTTPAuthorizationCredentials | None) -> str:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Authorization Bearer requis",
                            headers={"WWW-Authenticate": "Bearer"})
    return creds.credentials


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> TokenData:
    token = _extract_bearer(creds)
    payload = _decode_jwt(token)
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token de type invalide")
    # Vérifier que l'entreprise est active
    company = db.get_company(payload["company_id"])
    if not company or company["status"] != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Compte suspendu ou introuvable")
    return TokenData(payload)


async def get_admin_user(
    current: Annotated[TokenData, Depends(get_current_user)]
) -> TokenData:
    if not current.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Rôle admin requis")
    return current


async def get_superadmin_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> TokenData:
    token = _extract_bearer(creds)
    payload = _decode_jwt(token)
    if payload.get("role") != "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Rôle superadmin requis")
    return TokenData(payload)


# ─── Compatibilité ascendante : API Key header (optionnel) ───────────────────
# Permet aux clients Flutter anciens de continuer à fonctionner en parallèle
# pendant la migration. Retourne un TokenData minimal.
from fastapi.security import APIKeyHeader as _APIKeyHeader
from fastapi import Security as _Security

_api_key_header = _APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user_or_apikey(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, _Security(_api_key_header)] = None,
) -> TokenData:
    """
    Accepte soit un Bearer JWT (nouveau) soit un X-API-Key (legacy).
    """
    if creds:
        return await get_current_user(creds)

    if api_key:
        company = db.get_company_by_secret_key(api_key.strip())
        if company:
            return TokenData({
                "sub": "legacy",
                "company_id": company["id"],
                "role": "admin",
                "type": "access",
                "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
            })

    raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                        "Bearer JWT ou X-API-Key requis",
                        headers={"WWW-Authenticate": "Bearer"})
