"""
main.py — QuickSellPay v4.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Nouveautés v4 :
  • Auth JWT réel   (POST /auth/register|login|refresh|invite|accept-invite)
  • Multi-tenant    (une DB SQLite par boutique)
  • Abonnements     (GET|POST /billing/*)
  • Quotas par plan (produits / users / transactions)
  • Super-admin     (/admin/companies, /admin/stats)
  • Rate limiting   (/api/verify et /auth/login)
  • CORS restreint  (domaines du .env)
  • .env obligatoire pour secrets
  • Rétrocompat API Key X-API-Key (Flutter non migré)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import uuid, random, string, httpx, secrets, re, csv, io, zipfile
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Annotated, List
from xml.etree import ElementTree as ET

# ─── Sentry — monitoring des erreurs (configurer SENTRY_DSN dans .env) ───────
try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    _sentry_ready = True
except ImportError:
    _sentry_ready = False

from fastapi import (
    FastAPI, HTTPException, Depends, Request,
    UploadFile, File, status, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from config import get_settings
import database as db
from auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, create_invite_token,
    get_current_user, get_admin_user, get_superadmin_user,
    get_current_user_or_apikey,
    revoke_token,
    TokenData,
    _decode_jwt,
)
from models import *
from rate_limit import make_limiter
from storage import delete_company_assets, save_company_logo, save_product_image
from email_utils import send_email
import fedapay as feda
from quota import (
    check_product_quota, check_user_quota,
    check_transaction_quota, check_subscription_active,
)

cfg = get_settings()
cfg.validate_security()

# ─── Sentry init ──────────────────────────────────────────────────────────────
if _sentry_ready and cfg.SENTRY_DSN:
    sentry_sdk.init(
        dsn=cfg.SENTRY_DSN,
        integrations=[
            StarletteIntegration(transaction_style="url"),
            FastApiIntegration(transaction_style="url"),
        ],
        traces_sample_rate=cfg.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,  # RGPD : pas de données perso dans les traces
    )
    print(f"[Sentry] Monitoring activé (sample={cfg.SENTRY_TRACES_SAMPLE_RATE})")
else:
    print("[Sentry] Désactivé (SENTRY_DSN non configuré)")

# ─── Paths ────────────────────────────────────────────────────────────────────
# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR   = BASE_DIR / "static"
PUBLIC_DIR   = BASE_DIR / "public"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "images").mkdir(exist_ok=True)

# ─── Rate limiters ────────────────────────────────────────────────────────────
_rl_verify = make_limiter(cfg.RATE_LIMIT_VERIFY_REQUESTS, cfg.RATE_LIMIT_VERIFY_WINDOW)
_rl_login  = make_limiter(cfg.RATE_LIMIT_LOGIN_REQUESTS,  cfg.RATE_LIMIT_LOGIN_WINDOW)

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="QuickSellPay",
    description="Gestion stock, ventes & authenticité produits — multi-tenant SaaS",
    version="5.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'"
    if cfg.APP_ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")


def _generate_company_secret_key() -> str:
    return secrets.token_urlsafe(32)


def _is_vat_registered(value) -> bool:
    """Interprète sans ambiguïté le régime TVA issu de MySQL/SQLite."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "oui", "on"}
    return bool(value)


def _public_asset_url(value: str | None) -> str | None:
    """Remplace uniquement l'ancien domaine CDN de démonstration."""
    if not value:
        return value
    legacy_public_base = "https://cdn.quicksellpay.com/"
    public_base = (cfg.STORAGE_PUBLIC_BASE_URL or "").rstrip("/") + "/"
    if public_base != "/" and value.startswith(legacy_public_base):
        return public_base + value[len(legacy_public_base):]
    return value


def _ensure_company_secret(company_id: str) -> str:
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    secret_key = (company.get("secret_key") or "").strip()
    if secret_key:
        return secret_key
    secret_key = _generate_company_secret_key()
    db.update_company_secret_key(company_id, secret_key)
    return secret_key


def _token_response_for_user(user: dict, company: dict | None = None) -> TokenResponse:
    company = company or db.get_company(user["company_id"])
    secret_key = _ensure_company_secret(user["company_id"])
    return TokenResponse(
        access_token=create_access_token(user["id"], user["company_id"], user["role"]),
        refresh_token=create_refresh_token(user["id"], user["company_id"], user["role"]),
        user_id=user["id"],
        company_id=user["company_id"],
        company_name=company["name"] if company else "",
        company_logo_url=_public_asset_url(company.get("logo_url")) if company else None,
        secret_key=secret_key,
        role=user["role"],
        plan=db.get_active_plan(user["company_id"]),
        commercial_name=company.get("commercial_name") if company else None,
        rccm=company.get("rccm") if company else None,
        ifu=company.get("ifu") if company else None,
        address=company.get("address") if company else None,
        phone=company.get("phone") if company else None,
        contact_email=company.get("contact_email") if company else None,
        is_vat_registered=_is_vat_registered(company.get("is_vat_registered", True)) if company else True,
        mecef_token=company.get("mecef_token") if company else None,
        low_stock_threshold=int(company.get("low_stock_threshold") or 10) if company else 10,
    )


def _branding_payload(company_id: str) -> CompanyBrandingOut:
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    return CompanyBrandingOut(
        company_id=company["id"],
        company_name=company["name"],
        company_logo_url=_public_asset_url(company.get("logo_url")),
        commercial_name=company.get("commercial_name"),
        rccm=company.get("rccm"),
        ifu=company.get("ifu"),
        address=company.get("address"),
        phone=company.get("phone"),
        contact_email=company.get("contact_email"),
        is_vat_registered=_is_vat_registered(company.get("is_vat_registered", True)),
        mecef_token=company.get("mecef_token"),
        low_stock_threshold=int(company.get("low_stock_threshold") or 10),
    )


def _absolute_url(path: str) -> str:
    base = cfg.PUBLIC_APP_BASE_URL.rstrip("/")
    return f"{base}{path}"


def _ensure_password_confirmation(password: str, confirm_password: str):
    if password != confirm_password:
        raise HTTPException(400, "Les mots de passe ne correspondent pas")


def _resolve_login_user(identifier: str) -> dict | None:
    value = identifier.strip().lower()
    if not value:
        return None
    if "@" in value:
        return db.get_user_by_email(value)
    company = db.get_company_by_id_or_email(value)
    if not company:
        return None
    return db.get_primary_user_for_company(company["id"])


def _build_company_id(company_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", company_name.strip().lower()).strip("-")
    base = base or "boutique"
    candidate = base[:40]
    suffix = 1
    while db.get_company(candidate):
        suffix += 1
        candidate = f"{base[:32]}-{suffix}"
    return candidate


def _send_verification_email(user_id: str, email: str, company_name: str) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(tz=timezone.utc) + timedelta(
        hours=cfg.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS
    )).isoformat()
    db.insert_email_verification_token({
        "token": token,
        "user_id": user_id,
        "email": email,
        "expires_at": expires_at,
        "used": 0,
    })
    link = _absolute_url(f"/verify-email?token={token}")
    return send_email(
        to_email=email,
        subject="Verification de votre email",
        text=(
            f"Bonjour,\n\n"
            f"Confirmez l'email de votre boutique {company_name} : {link}\n\n"
            f"Ce lien expire dans {cfg.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS} heures."
        ),
        html=(
            f"<p>Bonjour,</p>"
            f"<p>Confirmez l'email de votre boutique <strong>{company_name}</strong> :</p>"
            f"<p><a href=\"{link}\">{link}</a></p>"
            f"<p>Ce lien expire dans {cfg.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS} heures.</p>"
        ),
    )


def _send_reset_password_email(user: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(tz=timezone.utc) + timedelta(
        minutes=cfg.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )).isoformat()
    db.clear_password_reset_tokens_for_user(user["id"])
    db.insert_password_reset_token({
        "token": token,
        "user_id": user["id"],
        "email": user["email"],
        "expires_at": expires_at,
        "used": 0,
    })
    link = _absolute_url(f"/reset-password?token={token}")
    return send_email(
        to_email=user["email"],
        subject="Reinitialisation de votre mot de passe",
        text=(
            f"Bonjour,\n\n"
            f"Reinitialisez votre mot de passe ici : {link}\n\n"
            f"Ce lien expire dans {cfg.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES} minutes."
        ),
        html=(
            f"<p>Bonjour,</p>"
            f"<p>Reinitialisez votre mot de passe ici :</p>"
            f"<p><a href=\"{link}\">{link}</a></p>"
            f"<p>Ce lien expire dans {cfg.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES} minutes.</p>"
        ),
    )


# ════════════════════════════════════════════════════════════════════════════
# PAGES HTML
# ════════════════════════════════════════════════════════════════════════════

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    p = FRONTEND_DIR / "favicon.ico"
    if p.exists():
        return FileResponse(p)
    raise HTTPException(status_code=404, detail="favicon not found")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_landing():
    return HTMLResponse((FRONTEND_DIR / "landing.html").read_text(encoding="utf-8"))


@app.get("/app",      response_class=HTMLResponse, include_in_schema=False)
@app.get("/app.html", response_class=HTMLResponse, include_in_schema=False)
def page_dashboard():
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/register",      response_class=HTMLResponse, include_in_schema=False)
@app.get("/register.html", response_class=HTMLResponse, include_in_schema=False)
def page_register():
    return HTMLResponse((FRONTEND_DIR / "register.html").read_text(encoding="utf-8"))


@app.get("/download/apk", include_in_schema=False)
def download_apk(arch: str = "arm64"):
    arch_map = {
        "arm64":  "QuickSellPay.apk",
        "armv7":  "QuickSellPay.apk",
        "x86_64": "QuickSellPay.apk",
    }
    filename = arch_map.get(arch, "QuickSellPay.apk")
    apk_path = STATIC_DIR / "downloads" / filename
    if not apk_path.exists():
        # Un lien public ne doit jamais afficher le JSON brut de FastAPI.
        return HTMLResponse(
            """<!doctype html><html lang='fr'><meta charset='utf-8'>
            <title>APK momentanément indisponible</title>
            <body style='font-family:Arial,sans-serif;max-width:560px;margin:12vh auto;padding:28px;color:#0f172a'>
              <h1>APK momentanément indisponible</h1>
              <p>La nouvelle version est en cours de publication. Réessayez dans quelques instants.</p>
              <a href='/' style='color:#009b8d'>Retour à QuickSellPay</a>
            </body></html>""",
            status_code=503,
        )
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename="QuickSellPay.apk",
    )

@app.get("/superadmin", response_class=HTMLResponse, include_in_schema=False)
def page_superadmin():
    return HTMLResponse(
        (FRONTEND_DIR / "superadmin.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )

@app.get("/verify",      response_class=HTMLResponse, include_in_schema=False)
@app.get("/verify.html", response_class=HTMLResponse, include_in_schema=False)
def page_verify():
    return HTMLResponse((FRONTEND_DIR / "verify.html").read_text(encoding="utf-8"))


@app.get("/about", response_class=HTMLResponse, include_in_schema=False)
def page_about():
    return HTMLResponse((FRONTEND_DIR / "about.html").read_text(encoding="utf-8"))


@app.get("/legal", response_class=HTMLResponse, include_in_schema=False)
def page_legal():
    return HTMLResponse((FRONTEND_DIR / "legal.html").read_text(encoding="utf-8"))


@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def page_terms():
    return HTMLResponse((FRONTEND_DIR / "terms.html").read_text(encoding="utf-8"))


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def page_privacy():
    return HTMLResponse((FRONTEND_DIR / "privacy.html").read_text(encoding="utf-8"))


@app.get("/billing",       response_class=HTMLResponse, include_in_schema=False)
@app.get("/billing.html",  response_class=HTMLResponse, include_in_schema=False)
def page_billing():
    return HTMLResponse((FRONTEND_DIR / "billing.html").read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"])
def health():
    return {
        "status":     "healthy",
        "version":    "5.0.0",
        "timestamp":  datetime.now().isoformat(),
        "db_engine":  "mysql" if __import__("db_driver").mysql_enabled() else "sqlite",
        "verify_url": "/verify",
    }


# ════════════════════════════════════════════════════════════════════════════
# AUTH  — inscription, connexion, refresh, invitations
# ════════════════════════════════════════════════════════════════════════════

def _ensure_superadmin():
    """Crée le super-admin au premier démarrage s'il n'existe pas."""
    company_id = "superadmin"
    now = datetime.now(tz=timezone.utc).isoformat()
    company = db.get_company(company_id)
    if not company:
        db.insert_company({
            "id": company_id, "name": "Super Admin",
            "email": cfg.SUPERADMIN_EMAIL,
            "secret_key": _generate_company_secret_key(),
            "logo_url": None,
            "commercial_name": "QuickSellPay Super Admin",
            "rccm": "NA",
            "ifu": "NA",
            "address": "Backoffice QuickSellPay",
            "phone": "0000000000",
            "contact_email": cfg.SUPERADMIN_EMAIL,
            "plan": "enterprise", "status": "active",
            "created_at": now,
        })
    else:
        _ensure_company_secret(company_id)
    superadmin = db.get_user_by_email(cfg.SUPERADMIN_EMAIL)
    if superadmin:
        if not verify_password(cfg.SUPERADMIN_PASSWORD, superadmin["password_hash"]):
            db.update_user_password(superadmin["id"], hash_password(cfg.SUPERADMIN_PASSWORD))
            db.revoke_user_tokens(superadmin["id"])
            print(f"[AUTH] Mot de passe super-admin synchronisé depuis .env : {cfg.SUPERADMIN_EMAIL}")
        return
    db.insert_user({
        "id":            str(uuid.uuid4()),
        "company_id":    company_id,
        "email":         cfg.SUPERADMIN_EMAIL,
        "password_hash": hash_password(cfg.SUPERADMIN_PASSWORD),
        "role":          "superadmin",
        "is_active":     1,
        "email_verified": 1,
        "created_at":    now,
    })
    print(f"[AUTH] Super-admin créé : {cfg.SUPERADMIN_EMAIL}")

_ensure_superadmin()


DEMO_COMPANY = {
    "id": "demo-shop",
    "name": "QuickSellPay Demo",
    "email": "demo@tpe-qr.com",
    "plan": "free",
    "status": "active",
    "commercial_name": "Boutique Demo",
    "rccm": "RCCM-DEMO-001",
    "ifu": "IFU-DEMO-001",
    "address": "Zone Demo, Porto-Novo",
    "phone": "+22900000000",
    "contact_email": "contact@demo.tpe-qr.com",
    "is_vat_registered": 1,
    "mecef_token": None,
}

DEMO_USERS = [
    {"email": "cashier@demo.tpe-qr.com", "password": "CashierDemo123!", "role": "employee", "label": "caissier"},
    {"email": "manager@demo.tpe-qr.com", "password": "ManagerDemo123!", "role": "manager", "label": "manager"},
    {"email": "admin@demo.tpe-qr.com", "password": "AdminDemo123!", "role": "admin", "label": "admin"},
]


def _ensure_demo_seed():
    now = datetime.now(tz=timezone.utc).isoformat()
    company = db.get_company(DEMO_COMPANY["id"])
    if not company:
        db.insert_company({
            **DEMO_COMPANY,
            "secret_key": _generate_company_secret_key(),
            "logo_url": None,
            "created_at": now,
        })
        db.init_tenant_db(DEMO_COMPANY["id"])
    secret_key = _ensure_company_secret(DEMO_COMPANY["id"])
    db.upsert_subscription({
        "id":                     str(uuid.uuid4()),
        "company_id":             DEMO_COMPANY["id"],
        "plan":                   DEMO_COMPANY["plan"],
        "status":                 "active",
        "start_date":             now,
        "end_date":               None,
        "stripe_subscription_id": None,
        "stripe_customer_id":     None,
        "updated_at":             now,
    })

    for demo_user in DEMO_USERS:
        if db.get_user_by_email(demo_user["email"]):
            continue
        db.insert_user({
            "id":            str(uuid.uuid4()),
            "company_id":    DEMO_COMPANY["id"],
            "email":         demo_user["email"],
            "password_hash": hash_password(demo_user["password"]),
            "role":          demo_user["role"],
            "is_active":     1,
            "email_verified": 1,
            "created_at":    now,
        })

    # Ne jamais écrire des mots de passe ou clés API dans les logs.
    print(f"[AUTH] Demo company ready: {DEMO_COMPANY['id']}")


if cfg.ENABLE_DEMO_DATA:
    _ensure_demo_seed()


@app.post("/auth/register", response_model=TokenResponse, status_code=201, tags=["Auth"])
def register(payload: RegisterRequest):
    """Inscription d'une nouvelle boutique + premier utilisateur admin."""
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    if db.get_company_by_email(payload.email):
        raise HTTPException(409, "Email déjà utilisé")

    now        = datetime.now(tz=timezone.utc).isoformat()
    company_id = _build_company_id(payload.company_name)
    user_id    = str(uuid.uuid4())
    secret_key = _generate_company_secret_key()

    db.insert_company({
        "id": company_id, "name": payload.company_name,
        "secret_key": secret_key,
        "logo_url": None,
        "commercial_name": payload.commercial_name,
        "rccm": payload.rccm,
        "ifu": payload.ifu,
        "address": payload.address,
        "phone": payload.phone,
        "contact_email": payload.contact_email,
        "is_vat_registered": int(payload.is_vat_registered),
        "mecef_token": payload.mecef_token,
        "email": payload.email, "plan": "free",
        "status": "active", "created_at": now,
    })
    # Créer la base tenant
    db.init_tenant_db(company_id)

    db.insert_user({
        "id":            user_id,
        "company_id":    company_id,
        "email":         payload.email,
        "password_hash": hash_password(payload.password),
        "role":          "admin",
        "is_active":     1,
        "email_verified": 0,
        "created_at":    now,
    })
    # Abonnement free par défaut
    db.upsert_subscription({
        "id":                     str(uuid.uuid4()),
        "company_id":             company_id,
        "plan":                   "free",
        "status":                 "active",
        "start_date":             now,
        "end_date":               None,
        "stripe_subscription_id": None,
        "stripe_customer_id":     None,
        "updated_at":             now,
    })

    mail_result = _send_verification_email(user_id, payload.email, payload.company_name)
    if not mail_result.get("sent") and mail_result.get("preview"):
        print(f"[AUTH] Verification email preview: {mail_result['preview']}")

    return _token_response_for_user({
        "id": user_id,
        "company_id": company_id,
        "role": "admin",
    }, {
        "id": company_id,
        "name": payload.company_name,
        "logo_url": None,
        "secret_key": secret_key,
        "commercial_name": payload.commercial_name,
        "rccm": payload.rccm,
        "ifu": payload.ifu,
        "address": payload.address,
        "phone": payload.phone,
        "contact_email": payload.contact_email,
        "is_vat_registered": int(payload.is_vat_registered),
        "mecef_token": payload.mecef_token,
    })


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(payload: LoginRequest, request: Request):
    """Connexion — retourne un JWT access + refresh."""
    _rl_login.check(request)

    user = _resolve_login_user(payload.identifier or "")
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Matricule, email ou mot de passe incorrect")
    if not user["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Compte désactivé")
    if user["company_status"] != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Boutique suspendue")

    company = db.get_company(user["company_id"])
    return _token_response_for_user(user, company)


@app.post("/auth/send-verification-email", response_model=ActionResponse, tags=["Auth"])
def resend_verification_email(payload: ForgotPasswordRequest):
    user = db.get_user_by_email(payload.email)
    if not user:
        return ActionResponse(message="Si l'adresse existe, un email de vérification sera envoyé.")
    if int(user.get("email_verified", 0)) == 1:
        return ActionResponse(message="Cette adresse email est déjà vérifiée.")
    result = _send_verification_email(
        user["id"],
        user["email"],
        user.get("company_name", "votre boutique"),
    )
    return ActionResponse(
        message="Email de vérification préparé.",
        preview=result.get("preview"),
    )


@app.post("/auth/verify-email", response_model=ActionResponse, tags=["Auth"])
def verify_email(payload: EmailVerificationRequest):
    row = db.get_email_verification_token(payload.token)
    if not row:
        raise HTTPException(400, "Lien de vérification invalide ou déjà utilisé")
    if datetime.now(tz=timezone.utc).isoformat() > row["expires_at"]:
        raise HTTPException(400, "Lien de vérification expiré")
    db.set_user_email_verified(row["user_id"], 1)
    db.mark_email_verification_token_used(payload.token)
    return ActionResponse(message="Email vérifié avec succès.")


@app.post("/auth/forgot-password", response_model=ActionResponse, tags=["Auth"])
def forgot_password(payload: ForgotPasswordRequest):
    user = db.get_user_by_email(payload.email)
    if not user:
        return ActionResponse(message="Si l'adresse existe, un email de réinitialisation sera envoyé.")
    _send_reset_password_email(user)
    # Réponse identique afin de ne pas divulguer l'état de la messagerie ou
    # un lien à usage unique.
    return ActionResponse(message="Si l'adresse existe, un email de réinitialisation sera envoyé.")


@app.post("/auth/reset-password", response_model=ActionResponse, tags=["Auth"])
def reset_password(payload: ResetPasswordRequest):
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    row = db.get_password_reset_token(payload.token)
    if not row:
        raise HTTPException(400, "Lien de réinitialisation invalide ou déjà utilisé")
    if datetime.now(tz=timezone.utc).isoformat() > row["expires_at"]:
        raise HTTPException(400, "Lien de réinitialisation expiré")
    db.update_user_password(row["user_id"], hash_password(payload.password))
    db.mark_password_reset_token_used(payload.token)
    db.revoke_user_tokens(row["user_id"])
    return ActionResponse(message="Mot de passe réinitialisé avec succès.")


@app.post("/auth/change-password", response_model=ActionResponse, tags=["Auth"])
def change_password(
    payload: ChangePasswordRequest,
    current: Annotated[TokenData, Depends(get_current_user)],
):
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    user = db.get_user_by_id(current.user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(400, "Ancien mot de passe incorrect")
    if payload.current_password == payload.password:
        raise HTTPException(400, "Le nouveau mot de passe doit etre different de l'ancien")
    db.update_user_password(current.user_id, hash_password(payload.password))
    db.revoke_user_tokens(current.user_id)
    return ActionResponse(message="Mot de passe mis a jour avec succes.")


@app.post("/auth/refresh", response_model=TokenResponse, tags=["Auth"])
def refresh(payload: RefreshRequest):
    """Renouvelle l'access token via le refresh token."""
    p = _decode_jwt(payload.refresh_token)
    if p.get("type") != "refresh":
        raise HTTPException(401, "Token de type invalide")
    # Vérifier que le refresh token n'est pas révoqué
    jti     = p.get("jti")
    user_id = p.get("sub", "")
    if jti and db.is_token_blacklisted(jti):
        raise HTTPException(401, "Session révoquée. Veuillez vous reconnecter.")
    revoked_before = db.get_user_revoked_before(user_id)
    if revoked_before:
        token_iat = datetime.fromtimestamp(p.get("iat", 0), tz=timezone.utc).isoformat()
        if token_iat < revoked_before:
            raise HTTPException(401, "Session révoquée. Veuillez vous reconnecter.")
    user    = db.get_user_by_id(p["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(401, "Utilisateur inactif")
    # Blacklister l'ancien refresh token (rotation)
    revoke_token(payload.refresh_token)
    company = db.get_company(p["company_id"])
    return _token_response_for_user(user, company)


@app.post("/auth/logout", response_model=None, status_code=204, tags=["Auth"])
def logout(
    request: Request,
    current: Annotated[TokenData, Depends(get_current_user)],
):
    """
    Déconnexion serveur-side : blackliste le token courant.
    Passer optionnellement le refresh_token dans le corps pour le révoquer aussi.
    """
    # Révoquer l'access token
    raw_access = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if raw_access:
        revoke_token(raw_access)

    # Nettoyage périodique de la blacklist (1% des requêtes logout)
    import random as _rnd
    if _rnd.random() < 0.01:
        db.cleanup_blacklist()


@app.post("/auth/invite", status_code=201, tags=["Auth"])
def invite_user(
    payload: InviteRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    """Invite un employé dans la boutique (admin requis)."""
    existing_user = db.get_user_by_email(payload.email)
    if existing_user:
        if existing_user["company_id"] == current.company_id:
            raise HTTPException(409, "Cet email est deja rattache a votre boutique")
        raise HTTPException(409, "Cet email est deja utilise sur la plateforme")
    check_user_quota(current.company_id)
    token = create_invite_token(current.company_id, payload.email, payload.role)
    _audit(
        current.company_id, current, "user_invited", "user_invite",
        object_label=payload.email,
        details={"email": payload.email, "role": payload.role},
    )
    company   = db.get_company(current.company_id)
    shop_name = company["name"] if company else current.company_id
    # /app contient le formulaire d'acceptation de l'invitation. La landing
    # page (/) ne le contient pas et ne doit pas recevoir ce lien.
    invite_link = _absolute_url(f"/app?invite_token={token}")
    mail_result = send_email(
        to_email=payload.email,
        subject=f"Invitation a rejoindre {shop_name} sur QuickSellPay",
        text=(
            f"Bonjour,\n\n"
            f"Vous avez ete invite(e) a rejoindre la boutique \"{shop_name}\" sur QuickSellPay "
            f"en tant que {payload.role}.\n\n"
            f"Cliquez sur le lien ci-dessous pour accepter l'invitation et definir votre mot de passe :\n"
            f"{invite_link}\n\n"
            f"Ce lien expire dans {cfg.INVITE_TOKEN_EXPIRE_HOURS} heures.\n\n"
            f"Si vous ne connaissez pas cette boutique, ignorez ce message.\n\n"
            f"L'equipe QuickSellPay"
        ),
        html=(
            f"<p>Bonjour,</p>"
            f"<p>Vous avez ete invite(e) a rejoindre la boutique <strong>{shop_name}</strong> sur QuickSellPay "
            f"en tant que <em>{payload.role}</em>.</p>"
            f"<p><a href='{invite_link}' style='display:inline-block;padding:10px 20px;"
            f"background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold;'>"
            f"Accepter l'invitation</a></p>"
            f"<p>Ou copiez ce lien dans votre navigateur :<br><small>{invite_link}</small></p>"
            f"<p>Ce lien expire dans <strong>{cfg.INVITE_TOKEN_EXPIRE_HOURS} heures</strong>.</p>"
            f"<p style='color:#888;font-size:12px;'>Si vous ne connaissez pas cette boutique, ignorez ce message.</p>"
        ),
    )
    return {
        "invite_token": token,
        "invite_link":  invite_link,
        "email":        payload.email,
        "role":         payload.role,
        "expires_in":   f"{cfg.INVITE_TOKEN_EXPIRE_HOURS}h",
        "email_sent":   mail_result.get("sent", False),
        "email_error":  None if mail_result.get("sent") else (
            "Brevo n'a pas accepté l'envoi. Vérifiez l'expéditeur validé dans Brevo et les journaux du serveur."
        ),
    }


@app.post("/auth/accept-invite", response_model=TokenResponse, tags=["Auth"])
def accept_invite(payload: AcceptInviteRequest):
    """L'employé définit son mot de passe via le token d'invitation."""
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    invite = db.get_invite(payload.token)
    if not invite:
        raise HTTPException(400, "Token invalide ou expiré")
    from datetime import datetime, timezone
    if datetime.now(tz=timezone.utc).isoformat() > invite["expires_at"]:
        raise HTTPException(400, "Invitation expirée")
    if db.get_user_by_email(invite["email"]):
        raise HTTPException(409, "Email déjà utilisé")

    now     = datetime.now(tz=timezone.utc).isoformat()
    user_id = str(uuid.uuid4())
    db.insert_user({
        "id":            user_id,
        "company_id":    invite["company_id"],
        "email":         invite["email"],
        "password_hash": hash_password(payload.password),
        "role":          invite["role"],
        "is_active":     1,
        "email_verified": 1,
        "created_at":    now,
    })
    db.mark_invite_used(payload.token)

    company = db.get_company(invite["company_id"])
    return _token_response_for_user({
        "id": user_id,
        "company_id": invite["company_id"],
        "role": invite["role"],
    }, company)


@app.get("/auth/demo-credentials", tags=["Auth"])
def demo_credentials():
    # Cette route existait pour les captures de démo. Elle ne doit jamais
    # exposer d'identifiants, même lorsqu'un environnement local active la démo.
    raise HTTPException(404, "Route indisponible")


@app.get("/auth/me", response_model=UserOut, tags=["Auth"])
def me(current: Annotated[TokenData, Depends(get_current_user)]):
    user = db.get_user_by_id(current.user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    return user


@app.get("/auth/company-profile", response_model=CompanyBrandingOut, tags=["Auth"])
def company_profile(current: Annotated[TokenData, Depends(get_current_user)]):
    return _branding_payload(current.company_id)


@app.patch("/auth/company-profile", response_model=CompanyBrandingOut, tags=["Auth"])
def update_company_profile(
    payload: CompanyProfileUpdate,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    company = db.get_company(current.company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    next_name = payload.company_name or company.get("name") or ""
    if len(next_name.strip()) < 2:
        raise HTTPException(400, "Le nom de la boutique est requis")
    db.update_company_profile(
        current.company_id,
        {
            "name": next_name,
            "commercial_name": payload.commercial_name if payload.commercial_name is not None else company.get("commercial_name"),
            "rccm": payload.rccm if payload.rccm is not None else company.get("rccm"),
            "ifu": payload.ifu if payload.ifu is not None else company.get("ifu"),
            "address": payload.address if payload.address is not None else company.get("address"),
            "phone": payload.phone if payload.phone is not None else company.get("phone"),
            "contact_email": payload.contact_email if payload.contact_email is not None else company.get("contact_email"),
            "is_vat_registered": int(payload.is_vat_registered) if payload.is_vat_registered is not None else company.get("is_vat_registered", 1),
            "mecef_token": payload.mecef_token if payload.mecef_token is not None else company.get("mecef_token"),
            "low_stock_threshold": payload.low_stock_threshold if payload.low_stock_threshold is not None else company.get("low_stock_threshold", 10),
        },
    )
    return _branding_payload(current.company_id)


@app.post("/auth/company-logo", response_model=CompanyBrandingOut, tags=["Auth"])
async def upload_company_logo(
    request: Request,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    form = await request.form()
    file: UploadFile = form.get("file")
    if not file:
        raise HTTPException(400, "Champ 'file' manquant")
    logo_url = save_company_logo(current.company_id, file, STATIC_DIR)
    db.update_company_logo(current.company_id, logo_url)
    return _branding_payload(current.company_id)

@app.get("/auth/users", response_model=List[UserOut], tags=["Auth"])
def list_users(current: Annotated[TokenData, Depends(get_admin_user)]):
    return db.list_users_for_company(current.company_id)

@app.delete("/auth/users/{user_id}", tags=["Auth"])
def deactivate_user(
    user_id: str,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    user = db.get_user_by_id(user_id)
    if not user or user["company_id"] != current.company_id:
        raise HTTPException(404, "Utilisateur introuvable")
    db.deactivate_user(user_id)
    _audit(
        current.company_id, current, "user_deactivated", "user",
        object_id=user_id, object_label=user["email"],
        details={"target_email": user["email"], "target_role": user["role"]},
    )
    return {"message": "Utilisateur désactivé"}

@app.patch("/auth/users/{user_id}/activate", tags=["Auth"])
def activate_user(
    user_id: str,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    user = db.get_user_by_id(user_id)
    if not user or user["company_id"] != current.company_id:
        raise HTTPException(404, "Utilisateur introuvable")
    if user["is_active"]:
        return {"message": "Utilisateur déjà actif"}
    check_user_quota(current.company_id)
    db.activate_user(user_id)
    _audit(
        current.company_id, current, "user_activated", "user",
        object_id=user_id, object_label=user["email"],
        details={"target_email": user["email"], "target_role": user["role"]},
    )
    return {"message": "Utilisateur activé"}

@app.get("/auth/audit-logs", tags=["Auth"])
def audit_logs(
    current: Annotated[TokenData, Depends(get_admin_user)],
    user_id: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    # Compatibilité avec les anciens clients : sans page, le tableau reste une liste.
    if page is None:
        return db.list_audit_logs(current.company_id, user_id=user_id, limit=limit or 100)
    items, total = db.page_audit_logs(current.company_id, user_id=user_id, page=page, per_page=per_page)
    return {"items": items, "page": page, "per_page": per_page, "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page)}


# ════════════════════════════════════════════════════════════════════════════
# BILLING
# ════════════════════════════════════════════════════════════════════════════

@app.get("/billing/status", response_model=SubscriptionStatus, tags=["Billing"])
def billing_status(current: Annotated[TokenData, Depends(get_current_user)]):
    sub  = db.get_subscription(current.company_id)
    plan = db.get_active_plan(current.company_id)
    limits = cfg.PLAN_LIMITS.get(plan, cfg.PLAN_LIMITS["free"])
    end_date = sub["end_date"] if sub else None
    # Compatibilité des anciens abonnements créés avant l'ajout de l'échéance.
    if sub and not end_date and sub.get("plan") != "free" and sub.get("start_date"):
        try:
            end_date = (datetime.fromisoformat(sub["start_date"]) + timedelta(days=31)).isoformat()
        except (TypeError, ValueError):
            end_date = None
    return SubscriptionStatus(
        company_id=current.company_id,
        plan=plan,
        status=sub["status"] if sub else "active",
        start_date=sub["start_date"] if sub else None,
        end_date=end_date,
        limits=limits,
    )


# -----------------------------------------------------------------------
# BILLING - FEDAPAY  (MTN Mobile Money, Moov Money, cartes Afrique West)
# -----------------------------------------------------------------------

@app.post("/billing/fedapay/checkout", response_model=FedaPayCheckoutResponse, tags=["Billing"])
def fedapay_checkout(
    payload: FedaPayCheckoutRequest,
    current: Annotated[TokenData, Depends(get_current_user)],
):
    """Cree une transaction FedaPay et retourne l'URL de paiement."""
    check_subscription_active(current.company_id)
    company = db.get_company(current.company_id)
    customer_email = company.get("contact_email") or company.get("email") or current.email if company else current.email
    customer_name  = company.get("name") or current.email if company else current.email
    result = feda.create_transaction(
        plan=payload.plan,
        company_id=current.company_id,
        customer_email=customer_email,
        customer_name=customer_name,
        callback_url=payload.success_url,
        cancel_url=payload.cancel_url,
    )
    return FedaPayCheckoutResponse(**result)


def _activate_fedapay_subscription(company_id: str, plan: str, transaction_id: str) -> dict:
    now = datetime.now(tz=timezone.utc).isoformat()
    end_date = feda.subscription_end_date(days=31)
    existing = db.get_subscription(company_id) or {}
    db.upsert_subscription({
        "id":                      existing.get("id") or str(uuid.uuid4()),
        "company_id":              company_id,
        "plan":                    plan,
        "status":                  "active",
        "start_date":              now,
        "end_date":                end_date,
        "stripe_subscription_id":  f"fedapay_{transaction_id}",
        "stripe_customer_id":      existing.get("stripe_customer_id", ""),
        "updated_at":              now,
    })
    return {"plan": plan, "end_date": end_date}


@app.post("/billing/fedapay/confirm", tags=["Billing"])
def fedapay_confirm(
    payload: FedaPayConfirmRequest,
    current: Annotated[TokenData, Depends(get_current_user)],
):
    """Confirmation active au retour du navigateur, en complément du webhook."""
    transaction = feda.get_transaction(payload.transaction_id)
    metadata = transaction.get("custom_metadata") or transaction.get("metadata") or {}
    company_id = str(metadata.get("company_id") or "")
    plan = str(metadata.get("plan") or "")
    status_value = str(transaction.get("status") or "").lower()
    if company_id != current.company_id or plan != payload.plan:
        raise HTTPException(403, "Cette transaction ne correspond pas à votre boutique ou à ce plan.")
    if status_value not in ("approved", "success"):
        return {"activated": False, "status": status_value or "pending"}
    subscription = _activate_fedapay_subscription(current.company_id, plan, payload.transaction_id)
    return {"activated": True, "status": status_value, **subscription}


@app.post("/billing/fedapay/webhook", tags=["Billing"], include_in_schema=False)
async def fedapay_webhook(request: Request):
    """Webhook FedaPay : active ou desactive l'abonnement apres paiement."""
    import json as _json
    body_bytes = await request.body()
    sig = request.headers.get("X-Fedapay-Signature", "")
    if not feda.verify_webhook_signature(body_bytes, sig):
        raise HTTPException(400, "Signature FedaPay invalide")
    body = _json.loads(body_bytes)
    event = feda.parse_webhook_event(body)
    if event is None:
        return {"received": True, "action": "ignored"}
    evt  = event["event"]
    cid  = event["company_id"]
    plan = event["plan"]
    tx_id = event["tx_id"]
    if evt == "approved":
        subscription = _activate_fedapay_subscription(cid, plan, tx_id)
        return {"received": True, "action": "subscription_activated", **subscription}
    if evt == "declined":
        existing = db.get_subscription(cid) or {}
        db.upsert_subscription({
            **existing,
            "company_id": cid,
            "status":     "payment_failed",
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        })
        return {"received": True, "action": "payment_failed"}
    return {"received": True, "action": "noop"}



# ════════════════════════════════════════════════════════════════════════════
# SUPER-ADMIN
# ════════════════════════════════════════════════════════════════════════════

@app.get("/admin/companies", response_model=List[CompanyOut], tags=["Admin"])
def admin_list_companies(
    _: Annotated[TokenData, Depends(get_superadmin_user)]
):
    return db.all_companies()


@app.patch("/admin/companies/{company_id}/suspend", tags=["Admin"])
def admin_suspend(
    company_id: str,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    if not db.get_company(company_id):
        raise HTTPException(404, "Boutique introuvable")
    db.update_company_status(company_id, "suspended")
    return {"message": f"Boutique {company_id} suspendue"}


@app.patch("/admin/companies/{company_id}/activate", tags=["Admin"])
def admin_activate(
    company_id: str,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    if not db.get_company(company_id):
        raise HTTPException(404, "Boutique introuvable")
    db.update_company_status(company_id, "active")
    return {"message": f"Boutique {company_id} réactivée"}


@app.delete("/admin/companies/{company_id}", tags=["Admin"])
def admin_delete_company(
    company_id: str,
    payload: DeleteCompanyRequest,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    if company_id == "superadmin":
        raise HTTPException(403, "Le compte superadmin ne peut pas être supprimé")
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    expected_confirmation = f"SUPPRIMER {company_id}"
    if payload.confirmation != expected_confirmation:
        raise HTTPException(400, f"Saisissez exactement : {expected_confirmation}")

    # Les fichiers sont supprimés avant les enregistrements : en cas d'échec
    # du stockage, la boutique reste intacte et l'opération peut être relancée.
    try:
        delete_company_assets(company_id, STATIC_DIR)
        deleted = db.delete_company_permanently(company_id)
    except Exception as exc:
        raise HTTPException(500, "Suppression incomplète : aucune nouvelle action automatique n'a été lancée") from exc
    if not deleted:
        raise HTTPException(404, "Boutique introuvable")
    return {"message": f"Boutique {company.get('name') or company_id} supprimée définitivement"}


@app.patch("/admin/companies/{company_id}/plan", tags=["Admin"])
def admin_set_plan(
    company_id: str,
    plan: str,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    if plan not in cfg.PLAN_LIMITS:
        raise HTTPException(400, f"Plan invalide. Valeurs: {list(cfg.PLAN_LIMITS.keys())}")
    if not db.get_company(company_id):
        raise HTTPException(404, "Boutique introuvable")
    now = datetime.now(tz=timezone.utc).isoformat()
    end_date = None if plan == "free" else (datetime.now(tz=timezone.utc) + timedelta(days=31)).isoformat()
    db.upsert_subscription({
        "id":                     str(uuid.uuid4()),
        "company_id":             company_id,
        "plan":                   plan,
        "status":                 "active",
        "start_date":             now,
        "end_date":               end_date,
        "stripe_subscription_id": None,
        "stripe_customer_id":     None,
        "updated_at":             now,
    })
    return {"message": f"Plan de {company_id} mis à jour → {plan}"}


@app.get("/admin/stats", tags=["Admin"])
def admin_stats(_: Annotated[TokenData, Depends(get_superadmin_user)]):
    return db.global_stats()


@app.get("/admin/companies/{company_id}/users", response_model=List[UserOut], tags=["Admin"])
def admin_list_company_users(
    company_id: str,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    if not db.get_company(company_id):
        raise HTTPException(404, "Boutique introuvable")
    return db.list_users_for_company(company_id)


@app.patch("/admin/users/{user_id}/reset-password", tags=["Admin"])
def admin_reset_user_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    if user["role"] == "superadmin":
        raise HTTPException(400, "Utilisez la configuration superadmin pour ce compte")
    db.update_user_password(user_id, hash_password(payload.password))
    db.clear_password_reset_tokens_for_user(user_id)
    db.revoke_user_tokens(user_id)
    return {
        "message": f"Mot de passe reinitialise pour {user['email']}",
        "company_id": user["company_id"],
        "user_id": user_id,
        "user_email": user["email"],
    }


@app.patch("/admin/companies/{company_id}/reset-password", tags=["Admin"])
def admin_reset_company_password(
    company_id: str,
    payload: AdminResetPasswordRequest,
    _: Annotated[TokenData, Depends(get_superadmin_user)],
):
    _ensure_password_confirmation(payload.password, payload.confirm_password)
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    user = db.get_primary_user_for_company(company_id)
    if not user:
        raise HTTPException(404, "Aucun compte actif trouvé pour cette boutique")
    db.update_user_password(user["id"], hash_password(payload.password))
    db.clear_password_reset_tokens_for_user(user["id"])
    db.revoke_user_tokens(user["id"])
    return {
        "message": f"Mot de passe réinitialisé pour {company.get('name') or company_id}",
        "company_id": company_id,
        "login_identifier": company_id,
        "user_email": user.get("email"),
    }


# ════════════════════════════════════════════════════════════════════════════
# PRODUITS  (JWT requis — company_id extrait du token)
# ════════════════════════════════════════════════════════════════════════════

def _enrich(p: dict, company_id: str) -> dict:
    """Ajoute company_id au dict pour compatibilité Flutter."""
    # Corrige les URLs créées avant la configuration de l'URL publique R2.
    # Les données restent lisibles sans migration manuelle de MySQL.
    p["image_url"] = _public_asset_url(p.get("image_url"))
    p["reference_image_url"] = _public_asset_url(p.get("reference_image_url"))
    p["company_id"] = company_id
    return p

def _actor_info(current: TokenData) -> dict:
    user = db.get_user_by_id(current.user_id) if current.user_id != "legacy" else None
    return {
        "user_id": current.user_id if current.user_id != "legacy" else None,
        "user_email": user["email"] if user else "legacy-api-key",
        "user_role": current.role,
    }

def _audit(
    company_id: str,
    current: TokenData,
    action: str,
    object_type: str,
    object_id: str | None = None,
    object_label: str | None = None,
    details: dict | None = None,
):
    actor = _actor_info(current)
    db.insert_audit_log(company_id, {
        "id": str(uuid.uuid4()),
        "user_id": actor["user_id"],
        "user_email": actor["user_email"],
        "user_role": actor["user_role"],
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "object_label": object_label,
        "details": details or {},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    })


def _csv_import_rows(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _safe_zip_entries(entries) -> list:
    """Bloque les archives qui épuiseraient mémoire ou disque à l'extraction."""
    if len(entries) > 1_000:
        raise ValueError("Archive trop volumineuse : trop de fichiers.")
    total_size = sum(entry.file_size for entry in entries)
    if total_size > 50 * 1024 * 1024:
        raise ValueError("Archive trop volumineuse après décompression (maximum 50 Mo).")
    if any(entry.file_size > 10 * 1024 * 1024 for entry in entries):
        raise ValueError("Un fichier de l'archive dépasse 10 Mo après décompression.")
    return entries


def _xlsx_import_rows(raw: bytes) -> list[dict]:
    """Lit la premiere feuille d'un fichier XLSX sans dependance externe."""
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        _safe_zip_entries(book.infolist())
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            shared_root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in shared_root.findall("x:si", ns)]
        sheets = sorted(name for name in book.namelist()
                        if name.startswith("xl/worksheets/") and name.endswith(".xml"))
        if not sheets:
            raise ValueError("Feuille Excel introuvable")
        root = ET.fromstring(book.read(sheets[0]))
        rows = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            values = []
            for cell in row.findall("x:c", ns):
                cell_type = cell.get("t")
                value = cell.findtext("x:v", default="", namespaces=ns)
                if cell_type == "s" and value.isdigit():
                    value = shared[int(value)]
                elif cell_type == "inlineStr":
                    value = "".join(cell.find("x:is", ns).itertext()) if cell.find("x:is", ns) is not None else ""
                values.append(value)
            if any(str(value).strip() for value in values):
                rows.append(values)
    if not rows:
        return []
    headers = [str(value).strip().lower() for value in rows[0]]
    return [
        {headers[index]: str(value).strip() for index, value in enumerate(row) if index < len(headers)}
        for row in rows[1:]
    ]


class _ArchiveImageUpload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.file = io.BytesIO(content)


@app.post("/api/products/import", tags=["Produits"])
async def import_products_file(
    current: Annotated[TokenData, Depends(get_admin_user)],
    file: UploadFile = File(...),
):
    """Importe CSV, XLSX, ou ZIP (produits.xlsx + images/)."""
    check_subscription_active(current.company_id)
    filename = (file.filename or "").lower()
    if not filename.endswith((".csv", ".xlsx", ".zip")):
        raise HTTPException(415, "Utilisez un fichier CSV, XLSX ou ZIP.")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Le fichier est vide.")
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "Le fichier ne doit pas depasser 30 Mo.")

    image_files: dict[str, tuple[str, bytes]] = {}
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                entries = _safe_zip_entries(
                    [entry for entry in bundle.infolist() if not entry.is_dir()]
                )
                table = next(
                    (entry for entry in entries
                     if entry.filename.replace("\\", "/").lower() in {"produits.csv", "produits.xlsx"}),
                    None,
                )
                if table is None:
                    raise ValueError("Le ZIP doit contenir produits.csv ou produits.xlsx a sa racine.")
                table_bytes = bundle.read(table)
                for entry in entries:
                    normalized = entry.filename.replace("\\", "/")
                    if normalized.lower().startswith("images/"):
                        image_files[Path(normalized).name.lower()] = (Path(normalized).name, bundle.read(entry))
                rows = _xlsx_import_rows(table_bytes) if table.filename.lower().endswith(".xlsx") else _csv_import_rows(table_bytes)
        else:
            rows = _xlsx_import_rows(raw) if filename.endswith(".xlsx") else _csv_import_rows(raw)
    except (ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise HTTPException(400, f"Fichier d'import invalide : {exc}") from exc

    imported = images_imported = skipped = 0
    errors = []
    for line, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        try:
            price = float((row.get("price") or "").replace(" ", "").replace(",", "."))
            stock = int((row.get("stock") or "").strip())
            if not name or price < 0 or stock < 0:
                raise ValueError
        except ValueError:
            skipped += 1
            errors.append(f"Ligne {line} ignoree : name, price et stock sont requis.")
            continue

        try:
            product = create_product(
                ProductCreate(
                    name=name,
                    price=price,
                    stock=stock,
                    sku=(row.get("sku") or "").strip() or None,
                    description=(row.get("description") or "").strip() or None,
                    image_url=(row.get("image_url") or "").strip() or None,
                    reference_image_url=(row.get("image_url") or "").strip() or None,
                ),
                current,
            )
        except HTTPException as exc:
            skipped += 1
            errors.append(f"Ligne {line} ignoree : {exc.detail}")
            continue

        imported += 1
        image_name = (row.get("image_file") or "").replace("\\", "/").split("/")[-1].lower()
        if image_name and filename.endswith(".zip"):
            image = image_files.get(image_name)
            if image is None:
                errors.append(f"Ligne {line} : image introuvable dans images/ ({image_name}).")
                continue
            try:
                image_url = save_product_image(
                    current.company_id, product["id"],
                    _ArchiveImageUpload(image[0], image[1]), STATIC_DIR,
                )
                db.update_product_image(
                    current.company_id, product["id"], image_url, image_url, None,
                    datetime.now(tz=timezone.utc).isoformat(),
                )
                images_imported += 1
            except HTTPException as exc:
                errors.append(f"Ligne {line} : image non importee ({exc.detail}).")

    return {
        "imported": imported,
        "images_imported": images_imported,
        "skipped": skipped,
        "errors": errors[:20],
    }


@app.get("/api/products", tags=["Produits"])
def list_products(
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
    page: Optional[int] = Query(default=None, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=100),
):
    cid = current.company_id
    # Sans `page`, on conserve la réponse liste pour les anciens clients/API.
    if page is not None:
        items, total = db.page_products(cid, page=page, per_page=per_page, query=q)
        return {"items": [_enrich(p, cid) for p in items], "page": page,
                "per_page": per_page, "total": total,
                "total_pages": max(1, (total + per_page - 1) // per_page)}
    return [_enrich(p, cid) for p in db.all_products(cid)]


@app.get("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def get_product(
    product_id: str,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    p = db.get_product(current.company_id, product_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    return _enrich(p, current.company_id)


@app.post("/api/products", response_model=Product, status_code=201, tags=["Produits"])
def create_product(
    payload: ProductCreate,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    check_subscription_active(current.company_id)
    check_product_quota(current.company_id)
    existing = db.get_product_by_name(current.company_id, payload.name)
    if existing:
        raise HTTPException(409, "Un produit avec ce libellé existe déjà")
    now = datetime.now().isoformat()
    pid = str(uuid.uuid4())
    p = {
        "id":  pid,
        "sku": payload.sku or f"SKU-{pid[:8].upper()}",
        "name":        payload.name,
        "description": payload.description,
        "price":       payload.price,
        "stock":       payload.stock,
        "image_url":             payload.image_url,
        "reference_image_url":   payload.reference_image_url,
        "reference_image_hash":  payload.reference_image_hash,
        "consumer_code":         payload.consumer_code,
        "created_at": now,
        "updated_at": now,
    }
    db.insert_product(current.company_id, p)
    _audit(
        current.company_id, current, "product_created", "product",
        object_id=pid, object_label=p["name"],
        details={"sku": p["sku"], "price": p["price"], "stock": p["stock"]},
    )
    return _enrich(p, current.company_id)


@app.put("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def update_product(
    product_id: str,
    payload: ProductUpdate,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    p = db.get_product(current.company_id, product_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    p.update({k: v for k, v in payload.model_dump(exclude_unset=True).items()})
    if payload.name is not None:
        existing = db.get_product_by_name(current.company_id, payload.name)
        if existing and existing["id"] != product_id:
            raise HTTPException(409, "Un produit avec ce libellé existe déjà")
    p["updated_at"] = datetime.now().isoformat()
    db.update_product_full(current.company_id, p)
    _audit(
        current.company_id, current, "product_updated", "product",
        object_id=product_id, object_label=p["name"],
        details=payload.model_dump(exclude_unset=True),
    )
    return _enrich(p, current.company_id)


@app.delete("/api/products/{product_id}", tags=["Produits"])
def delete_product(
    product_id: str,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    p = db.get_product(current.company_id, product_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    db.delete_product(current.company_id, product_id)
    _audit(
        current.company_id, current, "product_deleted", "product",
        object_id=product_id, object_label=p["name"],
        details={"sku": p.get("sku")},
    )
    return {"message": "Produit supprimé"}


@app.post("/api/products/bulk-delete", tags=["Produits"])
def bulk_delete_products(
    payload: BulkDeleteProductsRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    deleted = db.delete_products(current.company_id, payload.product_ids)
    for product in deleted:
        _audit(
            current.company_id, current, "product_deleted", "product",
            object_id=product["id"], object_label=product["name"],
            details={"sku": product.get("sku"), "bulk": True},
        )
    return {"message": f"{len(deleted)} produit(s) supprimé(s)", "deleted": len(deleted)}


@app.patch("/api/products/{product_id}/stock", response_model=Product, tags=["Produits"])
def update_stock(
    product_id: str,
    payload: StockUpdateRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    p = db.get_product(current.company_id, product_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    new_stock = p["stock"] + payload.delta
    if new_stock < 0:
        raise HTTPException(400, f"Stock insuffisant (actuel: {p['stock']})")
    now = datetime.now().isoformat()
    db.update_stock(current.company_id, product_id, new_stock, now)
    p["stock"]      = new_stock
    p["updated_at"] = now
    _audit(
        current.company_id, current, "stock_updated", "product",
        object_id=product_id, object_label=p["name"],
        details={"delta": payload.delta, "reason": payload.reason, "new_stock": new_stock},
    )
    return _enrich(p, current.company_id)


@app.patch("/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
@app.post( "/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
async def update_product_image(
    product_id: str,
    request: Request,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    p = db.get_product(current.company_id, product_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    now = datetime.now().isoformat()
    ctype = request.headers.get("content-type", "")

    if "multipart/form-data" in ctype:
        form = await request.form()
        file: UploadFile = form.get("file")
        if not file:
            raise HTTPException(400, "Champ 'file' manquant")
        image_url = save_product_image(
            current.company_id,
            product_id,
            file,
            STATIC_DIR,
        )
        db.update_product_image(
            current.company_id, product_id,
            image_url, p.get("reference_image_url"),
            p.get("reference_image_hash"), now
        )
    else:
        body = await request.json()
        payload = ProductImageUpdate(**body)
        db.update_product_image(
            current.company_id, product_id,
            payload.image_url or p.get("image_url"),
            payload.reference_image_url  or p.get("reference_image_url"),
            payload.reference_image_hash or p.get("reference_image_hash"),
            now,
        )
    updated = db.get_product(current.company_id, product_id)
    _audit(
        current.company_id, current, "product_image_updated", "product",
        object_id=product_id, object_label=updated["name"] if updated else p["name"],
        details={"image_url": updated.get("image_url") if updated else None},
    )
    return _enrich(updated, current.company_id)


# ════════════════════════════════════════════════════════════════════════════
# VENTES
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/sales", tags=["Ventes"])
def list_sales(
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
    period: str = Query(default="all"),
    user_id: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
):
    selected_user_id = user_id if current.is_admin else current.user_id
    if selected_user_id == "legacy":
        selected_user_id = None
    if page is not None and period == "all":
        sales, total = db.page_sales(
            current.company_id,
            created_by_user_id=selected_user_id,
            page=page,
            per_page=per_page,
        )
        return {
            "items": sales,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
    sales = db.all_sales(current.company_id, created_by_user_id=selected_user_id)
    if period == "all":
        return sales

    now = datetime.now()
    if period == "daily":
        filtered = [
            sale for sale in sales
            if datetime.fromisoformat(sale["created_at"]).date() == now.date()
        ]
    elif period == "weekly":
        year, week, _ = now.isocalendar()
        filtered = [
            sale for sale in sales
            if datetime.fromisoformat(sale["created_at"]).isocalendar()[:2] == (year, week)
        ]
    elif period == "monthly":
        filtered = [
            sale for sale in sales
            if datetime.fromisoformat(sale["created_at"]).year == now.year
            and datetime.fromisoformat(sale["created_at"]).month == now.month
        ]
    else:
        raise HTTPException(400, "Le filtre doit être all, daily, weekly ou monthly")
    return filtered


@app.get("/api/sales/{sale_id}", response_model=Sale, tags=["Ventes"])
def get_sale(
    sale_id: str,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    s = db.get_sale(current.company_id, sale_id)
    if not s:
        raise HTTPException(404, "Vente introuvable")
    if not current.is_admin and s.get("created_by_user_id") != current.user_id:
        raise HTTPException(403, "Accès refusé à cette vente")
    return s


@app.post("/api/sales", response_model=Sale, status_code=201, tags=["Ventes"])
def create_sale(
    payload: SaleCreate,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    check_subscription_active(current.company_id)
    check_transaction_quota(current.company_id)
    cid = current.company_id
    total, items_ok = 0, []
    for item in payload.items:
        p = db.get_product(cid, item.product_id)
        if not p:
            raise HTTPException(404, f"Produit {item.product_id} introuvable")
        if p["stock"] < item.quantity:
            raise HTTPException(400, f"Stock insuffisant pour {p['name']}")
        sub = p["price"] * item.quantity
        total += sub
        items_ok.append({
            "product_id": item.product_id, "product_name": p["name"],
            "quantity": item.quantity, "unit_price": p["price"], "subtotal": sub,
        })
        db.update_stock(cid, item.product_id, p["stock"] - item.quantity, datetime.now().isoformat())

    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    actor = _actor_info(current)
    sale = {
        "id": sid,
        "reference": f"VTE-{datetime.now().strftime('%Y%m%d')}-{sid[:6].upper()}",
        "source": payload.source or "dashboard",
        "items": items_ok, "total": total,
        "customer": payload.customer, "note": payload.note,
        "created_by_user_id": actor["user_id"],
        "created_by_email": actor["user_email"],
        "created_by_role": actor["user_role"],
        "created_at": now,
    }
    db.insert_sale(cid, sale)
    _audit(
        cid, current, "sale_created", "sale",
        object_id=sid, object_label=sale["reference"],
        details={"total": total, "items_count": len(items_ok), "source": sale["source"]},
    )
    return sale


# ════════════════════════════════════════════════════════════════════════════
# WEBHOOK Flutter POS
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/webhook/sale", tags=["Webhook"])
def webhook_sale(
    payload: WebhookPayload,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    check_subscription_active(current.company_id)
    cid = current.company_id
    errors, processed, items_enriched = [], [], []
    total_calc = 0
    for item in payload.items:
        p = None
        if item.get("product_id"):
            p = db.get_product(cid, item["product_id"])
        if not p and item.get("sku"):
            p = db.get_product_by_sku(cid, item["sku"])
        qty = item.get("quantity", 1)
        if p:
            sub = p["price"] * qty
            total_calc += sub
            items_enriched.append({
                "product_id": p["id"], "product_name": p["name"],
                "sku": p.get("sku", ""), "quantity": qty,
                "unit_price": p["price"], "subtotal": sub,
            })
            processed.append({"product_id": p["id"], "name": p["name"],
                               "current_stock": p["stock"]})
        else:
            errors.append(f"Produit non trouvé: {item.get('product_id') or item.get('sku')}")
            items_enriched.append(item)

    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    actor = _actor_info(current)
    reference = payload.sale_reference or f"WH-{sid[:8].upper()}"
    total = total_calc if total_calc > 0 else (payload.total or 0)
    db.insert_sale(cid, {
        "id": sid,
        "reference": reference,
        "source": "flutter_pos", "items": items_enriched,
        "total": total,
        "customer": None, "note": None,
        "created_by_user_id": actor["user_id"],
        "created_by_email": actor["user_email"],
        "created_by_role": actor["user_role"],
        "created_at": now,
    })
    _audit(
        cid, current, "sale_created", "sale",
        object_id=sid, object_label=reference,
        details={"total": total, "items_count": len(items_enriched), "source": "flutter_pos", "errors": errors},
    )
    return {"success": len(errors) == 0, "sale_id": sid,
            "processed": processed, "errors": errors, "timestamp": now}


# ════════════════════════════════════════════════════════════════════════════
# CODES D'AUTHENTICITÉ  (JWT requis)
# ════════════════════════════════════════════════════════════════════════════

def _make_code() -> str:
    # 128 bits d'entropie cryptographique : les anciens codes à 10 chiffres
    # pouvaient être énumérés et random n'est pas destiné aux secrets.
    raw = secrets.token_hex(16).upper()
    return "-".join(raw[index:index + 8] for index in range(0, len(raw), 8))


@app.post("/api/products/{product_id}/codes/generate",
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"])
@app.post("/api/products/{product_id}/generate-codes",
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"],
          include_in_schema=False)
def generate_codes(
    product_id: str,
    payload: GenerateCodesRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    cid = current.company_id
    product = db.get_product(cid, product_id)
    if not product:
        raise HTTPException(404, "Produit introuvable")
    now, created = datetime.now().isoformat(), []
    for _ in range(payload.quantity):
        entry = {
            "id": str(uuid.uuid4()), "product_id": product_id,
            "code": _make_code(), "status": "active", "created_at": now,
        }
        db.insert_auth_code(cid, entry)
        created.append(entry)
    _audit(
        cid, current, "auth_codes_generated", "product",
        object_id=product_id, object_label=product["name"],
        details={"quantity": payload.quantity},
    )
    return created


@app.get("/api/products/{product_id}/codes",
         response_model=List[AuthCode], tags=["Codes Auth"])
def list_codes(
    product_id: str,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    cid = current.company_id
    if not db.get_product(cid, product_id):
        raise HTTPException(404, "Produit introuvable")
    return db.get_codes_for_product(cid, product_id)


# ════════════════════════════════════════════════════════════════════════════
# VÉRIFICATION CLIENT FINAL  (PUBLIC — sans auth)
# ════════════════════════════════════════════════════════════════════════════

async def _reverse_geocode(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json"},
                headers={"User-Agent": "ERP-TPE-QR/4.0"},
            )
            if r.status_code == 200:
                addr = r.json().get("address", {})
                return {
                    "city":    addr.get("city") or addr.get("town") or addr.get("village") or "",
                    "country": addr.get("country", ""),
                }
    except Exception:
        pass
    return {"city": None, "country": None}


async def _do_verify(code_raw: str, latitude, longitude, request: Request,
                     company_id: str | None = None) -> dict:
    """
    Logique de vérification centrale.
    Si company_id est fourni, cherche uniquement dans cette boutique.
    Sinon, cherche dans toutes les boutiques (QR scanner public).
    """
    code = code_raw.strip().upper()

    # Résolution company_id : chercher dans toutes les DBs tenant si non fourni
    found_company = None
    auth_code     = None
    product_from_consumer = None

    if company_id:
        candidates = [company_id]
    else:
        candidates = db.tenant_ids()

    for cid in candidates:
        ac = db.get_auth_code_by_value(cid, code)
        if ac:
            auth_code      = ac
            found_company  = cid
            break
        pfc = db.get_product_by_consumer_code(cid, code)
        if pfc:
            product_from_consumer = pfc
            found_company         = cid
            break

    if not auth_code and not product_from_consumer:
        return {
            "valid": False, "already_used": False,
            "fraud_attempt": False,
            "product_name": None, "product_image": None,
            "product_image_url": None, "product_description": None,
            "company_name": None, "company_email": None, "company_status": None,
            "verification_count": 0, "fraud_attempts": 0,
            "message": "Code invalide. Vérifiez la saisie ou contactez le vendeur.",
            "used_at": None,
            "location_consent": latitude is not None and longitude is not None,
        }

    company = db.get_company(found_company) if found_company else None
    raw_ip  = request.client.host if request.client else "unknown"
    anon_ip = ".".join(raw_ip.split(".")[:3] + ["xxx"]) if "." in raw_ip else raw_ip
    now     = datetime.now().isoformat()

    def _insert_verification_log(*, code_id, product_id, attempt_type, is_valid, is_fraud, note):
        db.insert_verification(found_company, {
            "id":          str(uuid.uuid4()),
            "code_id":     code_id,
            "product_id":  product_id,
            "verified_at": now,
            "latitude":    latitude,
            "longitude":   longitude,
            "city":        city,
            "country":     country,
            "ip_address":  anon_ip,
            "user_agent":  request.headers.get("user-agent", "")[:200],
            "code_value":  code,
            "attempt_type": attempt_type,
            "is_valid":    1 if is_valid else 0,
            "is_fraud":    1 if is_fraud else 0,
            "note":        note,
        })

    # Géolocalisation
    city = country = None
    if latitude is not None and longitude is not None:
        geo = await _reverse_geocode(latitude, longitude)
        city, country = geo["city"], geo["country"]

    if auth_code:
        product = db.get_product(found_company, auth_code["product_id"])
        if auth_code["status"] == "used":
            _insert_verification_log(
                code_id=auth_code["id"],
                product_id=auth_code["product_id"],
                attempt_type="fraud_reuse",
                is_valid=False,
                is_fraud=True,
                note="Code déjà consommé",
            )
            stats = db.auth_code_aggregate_stats(found_company)
            verification_count = db.get_codes_for_product(found_company, auth_code["product_id"])
            return {
                "valid": False, "already_used": True,
                "fraud_attempt": True,
                "product_name":        product["name"] if product else None,
                "product_image":       _public_asset_url(product.get("image_url")) if product else None,
                "product_image_url":   _public_asset_url(product.get("image_url")) if product else None,
                "product_description": product.get("description") if product else None,
                "company_name": company.get("name") if company else None,
                "company_email": company.get("email") if company else None,
                "company_status": company.get("status") if company else None,
                "verification_count": len(verification_count),
                "fraud_attempts": stats.get("fake_attempts", 0),
                "message": "Ce code a déjà été vérifié. Si vous venez d'acheter ce produit, il est peut-être contrefait.",
                "used_at": auth_code.get("verified_at") or now,
                "verified_at": now,
                "location_consent": latitude is not None and longitude is not None,
            }
        code_id    = auth_code["id"]
        product_id = auth_code["product_id"]
        db.mark_code_used(found_company, code_id)
    else:
        product    = product_from_consumer
        code_id    = None
        product_id = product["id"]

    if code_id:
        _insert_verification_log(
            code_id=code_id,
            product_id=product_id,
            attempt_type="valid",
            is_valid=True,
            is_fraud=False,
            note="Vérification authentique",
        )

    stats = db.auth_code_aggregate_stats(found_company)
    product_verifications = db.all_verifications(found_company)
    same_product_attempts = [
        row for row in product_verifications
        if row.get("product_id") == product_id
    ]

    return {
        "valid": True, "already_used": False,
        "fraud_attempt": False,
        "product_name":        product["name"],
        "product_description": product.get("description"),
        "product_image":       _public_asset_url(product.get("image_url")),
        "product_image_url":   _public_asset_url(product.get("image_url")),
        "company_name": company.get("name") if company else None,
        "company_email": company.get("email") if company else None,
        "company_status": company.get("status") if company else None,
        "verification_count": len(same_product_attempts),
        "fraud_attempts": stats.get("fake_attempts", 0),
        "message": f"Produit authentique — {product['name']}",
        "used_at":    now,
        "verified_at": now,
        "location_consent": latitude is not None and longitude is not None,
    }


@app.post("/api/verify", tags=["Client Final"])
async def verify_v1(request: Request):
    """Endpoint public — appelé par verify.html."""
    _rl_verify.check(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")
    # Collecte de géolocalisation temporairement désactivée.
    return await _do_verify(
        body.get("code", ""),
        None,
        None,
        request,
    )


@app.post("/api/public/verify", response_model=VerifyResponse, tags=["Client Final"])
async def verify_v2(payload: VerifyRequest, request: Request):
    _rl_verify.check(request)
    # Collecte de géolocalisation temporairement désactivée.
    result = await _do_verify(payload.code, None, None, request)
    return VerifyResponse(**result)


@app.get("/api/public/preview/{code}", tags=["Client Final"])
def preview_code(code: str):
    """Aperçu du produit avant vérification (ne consomme pas le code)."""
    clean = code.strip().upper()
    for cid in db.tenant_ids():
        ac  = db.get_auth_code_by_value(cid, clean)
        if ac:
            p = db.get_product(cid, ac["product_id"])
            return {"product_name": p["name"] if p else None,
                    "product_image_url": _public_asset_url(p.get("image_url")) if p else None,
                    "code_status": ac["status"]}
        p = db.get_product_by_consumer_code(cid, clean)
        if p:
            return {"product_name": p["name"],
                    "product_image_url": _public_asset_url(p.get("image_url")),
                    "code_status": "active"}
    raise HTTPException(404, "Code introuvable")


# ════════════════════════════════════════════════════════════════════════════
# STATS
# ════════════════════════════════════════════════════════════════════════════

def _pdf_response(title: str, headers: list[str], rows: list[list], metrics: list[tuple[str, str]] | None = None):
    """Construit un PDF téléchargeable localement."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except ImportError as exc:
        raise HTTPException(503, "Génération PDF indisponible : installez les dépendances du projet.") from exc
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=12 * mm, leftMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    content = [Paragraph(title, styles["Title"]), Paragraph(f"QuickSellPay — généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Normal"]), Spacer(1, 8)]
    if metrics:
        metric_table = Table([[f"{label} : {value}" for label, value in metrics]])
        metric_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf8f7")), ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#087f78")), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")), ("PADDING", (0, 0), (-1, -1), 8)]))
        content.extend([metric_table, Spacer(1, 10)])
    table_rows = [headers] + (rows or [["Aucune donnée"] + [""] * (len(headers) - 1)])
    table = Table(table_rows, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#087f78")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")), ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("PADDING", (0, 0), (-1, -1), 5)]))
    content.append(table)
    document.build(content)
    filename = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + ".pdf"
    return Response(content=buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/reports/dashboard.pdf", tags=["Rapports"])
def dashboard_pdf(current: Annotated[TokenData, Depends(get_current_user)]):
    products = db.all_products(current.company_id)
    sales = db.all_sales(
        current.company_id,
        created_by_user_id=None if current.is_admin else current.user_id,
    )
    threshold = int((db.get_company(current.company_id) or {}).get("low_stock_threshold") or 10)
    low_stock = [p for p in products if int(p.get("stock") or 0) <= threshold]
    rows = [[p["name"], p.get("sku") or "-", f"{p.get('price', 0):,.0f}", p.get("stock", 0), "Rupture" if int(p.get("stock") or 0) == 0 else "Stock faible"] for p in low_stock]
    return _pdf_response("État global du stock — alertes", ["Produit", "SKU", "Prix (FCFA)", "Stock", "Statut"], rows, [("Chiffre d’affaires", f"{sum(s.get('total', 0) for s in sales):,.0f} FCFA"), ("Ventes", str(len(sales))), ("Produits", str(len(products))), ("Alertes stock", str(len(low_stock)))])


@app.get("/api/reports/products.pdf", tags=["Rapports"])
def products_pdf(current: Annotated[TokenData, Depends(get_current_user)], q: str = Query(default="", max_length=100)):
    products = db.all_products(current.company_id)
    if q.strip():
        needle = q.strip().lower()
        products = [p for p in products if needle in p["name"].lower() or needle in (p.get("sku") or "").lower()]
    rows = [[p["name"], p.get("sku") or "-", f"{p.get('price', 0):,.0f}", p.get("stock", 0), "Rupture" if p.get("stock", 0) == 0 else ("Stock faible" if p.get("stock", 0) < 10 else "En stock")] for p in products]
    return _pdf_response("Liste des produits", ["Produit", "SKU", "Prix (FCFA)", "Stock", "Statut"], rows)


@app.get("/api/reports/sales.pdf", tags=["Rapports"])
def sales_pdf(current: Annotated[TokenData, Depends(get_current_user)], period: str = Query(default="all"), user_id: str | None = Query(default=None)):
    sales = list_sales(
        current=current,
        period=period,
        user_id=user_id,
        page=None,
        per_page=20,
    )
    rows = [[sale.get("reference") or "-", ", ".join(item.get("product_name") or item.get("name") or "-" for item in (sale.get("items") or [])) or "-", f"{sale.get('total', 0):,.0f}", sale.get("created_by_email") or "-", sale.get("created_at") or "-"] for sale in sales]
    return _pdf_response("Liste des ventes", ["Référence", "Produit(s)", "Total (FCFA)", "Employé", "Date"], rows)


@app.get("/api/stats", tags=["Stats"])
def get_stats(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    cid      = current.company_id
    products = db.all_products(cid)
    # Admins/managers voient les chiffres de la boutique. Un employé ne voit
    # que ses propres ventes et son propre chiffre d'affaires.
    sales_user_id = None if current.is_admin or current.user_id == "legacy" else current.user_id
    sales    = db.all_sales(cid, created_by_user_id=sales_user_id)
    verif    = db.get_verification_stats(cid)
    threshold = int((db.get_company(cid) or {}).get("low_stock_threshold") or 10)
    return {
        "total_products":  len(products),
        "total_sales":     len(sales),
        "total_revenue":   sum(s.get("total", 0) for s in sales),
        "low_stock_threshold": threshold,
        "low_stock_count": len([p for p in products if p["stock"] <= threshold]),
        "low_stock_items": [p for p in products if p["stock"] <= threshold][:5],
        "verifications":   verif,
    }

@app.get("/api/verifications", tags=["Stats"])
def all_verifs(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    return db.all_verifications(current.company_id)

@app.get("/api/stats/verifications", tags=["Stats"])
def verif_stats(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    return db.get_verification_stats(current.company_id)

@app.get("/api/authenticity/stats", tags=["Stats"], include_in_schema=False)
def auth_stats_alias(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    return db.auth_code_aggregate_stats(current.company_id)

@app.get("/api/authenticity/logs", tags=["Stats"], include_in_schema=False)
def auth_logs_alias(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    logs = db.all_verifications(current.company_id)
    return logs

@app.get("/api/products/{product_id}/verifications", tags=["Stats"])
def product_verifications(
    product_id: str,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    cid = current.company_id
    if not db.get_product(cid, product_id):
        raise HTTPException(404, "Produit introuvable")
    with db.get_conn(cid) as conn:
        rows = conn.execute(
            "SELECT v.*, a.code FROM verifications v "
            "JOIN auth_codes a ON v.code_id=a.id "
            "WHERE v.product_id=? ORDER BY v.verified_at DESC",
            (product_id,)
        ).fetchall()
    return [dict(r) for r in rows]
