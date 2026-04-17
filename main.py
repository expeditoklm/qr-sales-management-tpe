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

import uuid, random, string, httpx, secrets, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated, List

from fastapi import (
    FastAPI, HTTPException, Depends, Request,
    UploadFile, File, status, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings
import database as db
from auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, create_invite_token,
    get_current_user, get_admin_user, get_superadmin_user,
    get_current_user_or_apikey,
    TokenData,
    _decode_jwt,
)
from models import *
from rate_limit import make_limiter
from storage import save_company_logo, save_product_image
from email_utils import send_email
from quota import (
    check_product_quota, check_user_quota,
    check_transaction_quota, check_subscription_active,
)

cfg = get_settings()

# ─── Paths ────────────────────────────────────────────────────────────────────
# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR   = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "images").mkdir(exist_ok=True)

# ─── Rate limiters ────────────────────────────────────────────────────────────
_rl_verify = make_limiter(cfg.RATE_LIMIT_VERIFY_REQUESTS, cfg.RATE_LIMIT_VERIFY_WINDOW)
_rl_login  = make_limiter(cfg.RATE_LIMIT_LOGIN_REQUESTS,  cfg.RATE_LIMIT_LOGIN_WINDOW)

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="QuickSellPay",
    description="Gestion stock, ventes & authenticité produits — multi-tenant SaaS",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _generate_company_secret_key() -> str:
    return secrets.token_urlsafe(32)


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
        company_logo_url=company.get("logo_url") if company else None,
        secret_key=secret_key,
        role=user["role"],
        plan=db.get_active_plan(user["company_id"]),
        commercial_name=company.get("commercial_name") if company else None,
        rccm=company.get("rccm") if company else None,
        ifu=company.get("ifu") if company else None,
        address=company.get("address") if company else None,
        phone=company.get("phone") if company else None,
        contact_email=company.get("contact_email") if company else None,
    )


def _branding_payload(company_id: str) -> CompanyBrandingOut:
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Boutique introuvable")
    return CompanyBrandingOut(
        company_id=company["id"],
        company_name=company["name"],
        company_logo_url=company.get("logo_url"),
        commercial_name=company.get("commercial_name"),
        rccm=company.get("rccm"),
        ifu=company.get("ifu"),
        address=company.get("address"),
        phone=company.get("phone"),
        contact_email=company.get("contact_email"),
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
    return FileResponse(p) if p.exists() else HTTPException(404)

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_dashboard():
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))

@app.get("/superadmin", response_class=HTMLResponse, include_in_schema=False)
def page_superadmin():
    return HTMLResponse((FRONTEND_DIR / "superadmin.html").read_text(encoding="utf-8"))

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


# ════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"])
def health():
    return {
        "status":     "healthy",
        "version":    "4.0.0",
        "timestamp":  datetime.now().isoformat(),
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
    if db.get_user_by_email(cfg.SUPERADMIN_EMAIL):
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

    print("[AUTH] Demo company prête :")
    print(f"        company_id={DEMO_COMPANY['id']}")
    print(f"        secret_key={secret_key}")
    for demo_user in DEMO_USERS:
        print(
            f"        {demo_user['label']}: "
            f"{demo_user['email']} / {demo_user['password']}"
        )
    print(
        "        superadmin: "
        f"{cfg.SUPERADMIN_EMAIL} / {cfg.SUPERADMIN_PASSWORD}"
    )


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
    result = _send_reset_password_email(user)
    return ActionResponse(
        message="Email de réinitialisation préparé.",
        preview=result.get("preview"),
    )


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
    return ActionResponse(message="Mot de passe réinitialisé avec succès.")


@app.post("/auth/refresh", response_model=TokenResponse, tags=["Auth"])
def refresh(payload: RefreshRequest):
    """Renouvelle l'access token via le refresh token."""
    p = _decode_jwt(payload.refresh_token)
    if p.get("type") != "refresh":
        raise HTTPException(401, "Token de type invalide")
    user    = db.get_user_by_id(p["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(401, "Utilisateur inactif")
    company = db.get_company(p["company_id"])
    return _token_response_for_user(user, company)


@app.post("/auth/invite", status_code=201, tags=["Auth"])
def invite_user(
    payload: InviteRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    """Invite un employé dans la boutique (admin requis)."""
    check_user_quota(current.company_id)
    token = create_invite_token(current.company_id, payload.email, payload.role)
    # En prod : envoyer par email. Ici on retourne le lien.
    return {
        "invite_token": token,
        "invite_link":  f"/auth/accept-invite?token={token}",
        "email":        payload.email,
        "role":         payload.role,
        "expires_in":   f"{cfg.INVITE_TOKEN_EXPIRE_HOURS}h",
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
    company = db.get_company(DEMO_COMPANY["id"]) or {}
    return {
        "company_id": DEMO_COMPANY["id"],
        "company_name": DEMO_COMPANY["name"],
        "secret_key": company.get("secret_key", ""),
        "accounts": [
            {
                "label": demo_user["label"],
                "email": demo_user["email"],
                "password": demo_user["password"],
                "role": demo_user["role"],
            }
            for demo_user in DEMO_USERS
        ] + [{
            "label": "superadmin",
            "email": cfg.SUPERADMIN_EMAIL,
            "password": cfg.SUPERADMIN_PASSWORD,
            "role": "superadmin",
        }],
    }


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
        },
    )
    return _branding_payload(current.company_id)


@app.post("/auth/company-logo", response_model=CompanyBrandingOut, tags=["Auth"])
async def upload_company_logo(
    request: Request,
    current: Annotated[TokenData, Depends(get_current_user)],
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
    return {"message": "Utilisateur désactivé"}


# ════════════════════════════════════════════════════════════════════════════
# BILLING
# ════════════════════════════════════════════════════════════════════════════

@app.get("/billing/status", response_model=SubscriptionStatus, tags=["Billing"])
def billing_status(current: Annotated[TokenData, Depends(get_current_user)]):
    sub  = db.get_subscription(current.company_id)
    plan = db.get_active_plan(current.company_id)
    limits = cfg.PLAN_LIMITS.get(plan, cfg.PLAN_LIMITS["free"])
    return SubscriptionStatus(
        company_id=current.company_id,
        plan=plan,
        status=sub["status"] if sub else "active",
        start_date=sub["start_date"] if sub else None,
        end_date=sub["end_date"] if sub else None,
        limits=limits,
    )


def _stripe_price_to_plan_map() -> dict[str, str]:
    return {
        price_id: plan
        for plan, price_id in cfg.STRIPE_PRICES.items()
        if price_id
    }


def _stripe_plan_from_price_id(price_id: str | None) -> str | None:
    if not price_id:
        return None
    return _stripe_price_to_plan_map().get(price_id)


def _stripe_plan_from_subscription_object(obj: dict) -> str | None:
    items = ((obj.get("items") or {}).get("data") or [])
    for item in items:
        price = item.get("price") or {}
        plan = _stripe_plan_from_price_id(price.get("id"))
        if plan:
            return plan
    return None


@app.post("/billing/create-checkout-session", tags=["Billing"])
def create_checkout(
    payload: CreateCheckoutRequest,
    current: Annotated[TokenData, Depends(get_admin_user)],
):
    """Crée une session Stripe Checkout (nécessite STRIPE_SECRET_KEY dans .env)."""
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(501, "Stripe non configuré — ajoutez STRIPE_SECRET_KEY dans .env")
    price_id = cfg.STRIPE_PRICES.get(payload.plan, "")
    if not price_id:
        raise HTTPException(501, f"Prix Stripe non configuré pour le plan '{payload.plan}'")

    try:
        import stripe
        stripe.api_key = cfg.STRIPE_SECRET_KEY
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=payload.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=payload.cancel_url,
            metadata={"company_id": current.company_id, "plan": payload.plan},
        )
        return {"url": session.url, "session_id": session.id}
    except ImportError:
        raise HTTPException(501, "Package stripe non installé — pip install stripe")
    except Exception as e:
        raise HTTPException(500, f"Erreur Stripe: {e}")


@app.post("/billing/webhook", tags=["Billing"], include_in_schema=False)
async def stripe_webhook(request: Request):
    """Reçoit les événements Stripe (paiement, annulation…)."""
    if not cfg.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(501, "Stripe webhook non configuré")
    try:
        import stripe
        stripe.api_key = cfg.STRIPE_SECRET_KEY
        body      = await request.body()
        sig       = request.headers.get("stripe-signature", "")
        event     = stripe.Webhook.construct_event(body, sig, cfg.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Webhook invalide: {e}")

    now = datetime.now(tz=timezone.utc).isoformat()
    ev_type = event["type"]
    obj     = event["data"]["object"]

    if ev_type == "checkout.session.completed":
        company_id   = obj["metadata"].get("company_id")
        sub_id       = obj.get("subscription")
        customer_id  = obj.get("customer")
        plan         = obj.get("metadata", {}).get("plan")
        if not plan and obj.get("id"):
            try:
                line_items = stripe.checkout.Session.list_line_items(obj["id"], limit=10)
                for item in line_items.get("data", []):
                    price = item.get("price") or {}
                    plan = _stripe_plan_from_price_id(price.get("id"))
                    if plan:
                        break
            except Exception:
                plan = None
        if not plan:
            plan = "basic"
        if company_id:
            db.upsert_subscription({
                "id":                     str(uuid.uuid4()),
                "company_id":             company_id,
                "plan":                   "basic",  # à affiner via price_id
                "status":                 "active",
                "start_date":             now,
                "end_date":               None,
                "stripe_subscription_id": sub_id,
                "stripe_customer_id":     customer_id,
                "updated_at":             now,
            })
            if plan != "basic":
                sub = db.get_subscription(company_id)
                if sub:
                    db.upsert_subscription({
                        "id":                     sub["id"],
                        "company_id":             company_id,
                        "plan":                   plan,
                        "status":                 sub["status"],
                        "start_date":             sub["start_date"],
                        "end_date":               sub["end_date"],
                        "stripe_subscription_id": sub["stripe_subscription_id"],
                        "stripe_customer_id":     sub["stripe_customer_id"],
                        "updated_at":             now,
                    })

    elif ev_type in ("customer.subscription.updated",):
        sub_id = obj["id"]
        status_stripe = obj["status"]  # active, past_due, canceled…
        with db._shared_conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET status=?, updated_at=? WHERE stripe_subscription_id=?",
                (status_stripe, now, sub_id)
            )
        plan = _stripe_plan_from_subscription_object(obj)
        if plan:
            with db._shared_conn() as conn:
                conn.execute(
                    "UPDATE subscriptions SET plan=?, updated_at=? WHERE stripe_subscription_id=?",
                    (plan, now, sub_id)
                )

    elif ev_type == "customer.subscription.deleted":
        sub_id = obj["id"]
        with db._shared_conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET status='canceled', end_date=?, updated_at=? "
                "WHERE stripe_subscription_id=?",
                (now, now, sub_id)
            )

    return {"received": True}


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
    db.upsert_subscription({
        "id":                     str(uuid.uuid4()),
        "company_id":             company_id,
        "plan":                   plan,
        "status":                 "active",
        "start_date":             now,
        "end_date":               None,
        "stripe_subscription_id": None,
        "stripe_customer_id":     None,
        "updated_at":             now,
    })
    return {"message": f"Plan de {company_id} mis à jour → {plan}"}


@app.get("/admin/stats", tags=["Admin"])
def admin_stats(_: Annotated[TokenData, Depends(get_superadmin_user)]):
    return db.global_stats()


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
    p["company_id"] = company_id
    return p


@app.get("/api/products", response_model=List[Product], tags=["Produits"])
def list_products(
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)]
):
    cid = current.company_id
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
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
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
    return _enrich(p, current.company_id)


@app.put("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def update_product(
    product_id: str,
    payload: ProductUpdate,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
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
    return _enrich(p, current.company_id)


@app.delete("/api/products/{product_id}", tags=["Produits"])
def delete_product(
    product_id: str,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    if not db.get_product(current.company_id, product_id):
        raise HTTPException(404, "Produit introuvable")
    db.delete_product(current.company_id, product_id)
    return {"message": "Produit supprimé"}


@app.patch("/api/products/{product_id}/stock", response_model=Product, tags=["Produits"])
def update_stock(
    product_id: str,
    payload: StockUpdateRequest,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
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
    return _enrich(p, current.company_id)


@app.patch("/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
@app.post( "/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
async def update_product_image(
    product_id: str,
    request: Request,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
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
    return _enrich(db.get_product(current.company_id, product_id), current.company_id)


# ════════════════════════════════════════════════════════════════════════════
# VENTES
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/sales", response_model=List[Sale], tags=["Ventes"])
def list_sales(
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
    period: str = Query(default="all"),
):
    sales = db.all_sales(current.company_id)
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
    sale = {
        "id": sid,
        "reference": f"VTE-{datetime.now().strftime('%Y%m%d')}-{sid[:6].upper()}",
        "source": payload.source or "dashboard",
        "items": items_ok, "total": total,
        "customer": payload.customer, "note": payload.note,
        "created_at": now,
    }
    db.insert_sale(cid, sale)
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
    db.insert_sale(cid, {
        "id": sid,
        "reference": payload.sale_reference or f"WH-{sid[:8].upper()}",
        "source": "flutter_pos", "items": items_enriched,
        "total": total_calc if total_calc > 0 else (payload.total or 0),
        "customer": None, "note": None, "created_at": now,
    })
    return {"success": len(errors) == 0, "sale_id": sid,
            "processed": processed, "errors": errors, "timestamp": now}


# ════════════════════════════════════════════════════════════════════════════
# CODES D'AUTHENTICITÉ  (JWT requis)
# ════════════════════════════════════════════════════════════════════════════

def _make_code() -> str:
    while True:
        d = ''.join(random.choices(string.digits, k=10))
        return f"{d[:5]}-{d[5:]}"


@app.post("/api/products/{product_id}/codes/generate",
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"])
@app.post("/api/products/{product_id}/generate-codes",
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"],
          include_in_schema=False)
def generate_codes(
    product_id: str,
    payload: GenerateCodesRequest,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
):
    cid = current.company_id
    if not db.get_product(cid, product_id):
        raise HTTPException(404, "Produit introuvable")
    now, created = datetime.now().isoformat(), []
    for _ in range(payload.quantity):
        entry = {
            "id": str(uuid.uuid4()), "product_id": product_id,
            "code": _make_code(), "status": "active", "created_at": now,
        }
        db.insert_auth_code(cid, entry)
        created.append(entry)
    return created


@app.get("/api/products/{product_id}/codes",
         response_model=List[AuthCode], tags=["Codes Auth"])
def list_codes(
    product_id: str,
    current: Annotated[TokenData, Depends(get_current_user_or_apikey)],
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
        # Lister toutes les bases tenant
        from config import get_settings as _cfg
        candidates = [
            f.stem[4:]  # erp_{company_id}.db → company_id
            for f in _cfg().DATA_DIR.glob("erp_*.db")
        ]

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
                "product_image":       product.get("image_url") if product else None,
                "product_image_url":   product.get("image_url") if product else None,
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
        "product_image":       product.get("image_url"),
        "product_image_url":   product.get("image_url"),
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
    consent = bool(body.get("consent"))
    return await _do_verify(
        body.get("code", ""),
        body.get("latitude") if consent else None,
        body.get("longitude") if consent else None,
        request,
    )


@app.post("/api/public/verify", response_model=VerifyResponse, tags=["Client Final"])
async def verify_v2(payload: VerifyRequest, request: Request):
    _rl_verify.check(request)
    lat = payload.latitude  if payload.consent else None
    lon = payload.longitude if payload.consent else None
    result = await _do_verify(payload.code, lat, lon, request)
    return VerifyResponse(**result)


@app.get("/api/public/preview/{code}", tags=["Client Final"])
def preview_code(code: str):
    """Aperçu du produit avant vérification (ne consomme pas le code)."""
    clean = code.strip().upper()
    from config import get_settings as _cfg
    for db_file in _cfg().DATA_DIR.glob("erp_*.db"):
        cid = db_file.stem[4:]
        ac  = db.get_auth_code_by_value(cid, clean)
        if ac:
            p = db.get_product(cid, ac["product_id"])
            return {"product_name": p["name"] if p else None,
                    "product_image_url": p.get("image_url") if p else None,
                    "code_status": ac["status"]}
        p = db.get_product_by_consumer_code(cid, clean)
        if p:
            return {"product_name": p["name"],
                    "product_image_url": p.get("image_url"),
                    "code_status": "active"}
    raise HTTPException(404, "Code introuvable")


# ════════════════════════════════════════════════════════════════════════════
# STATS
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/stats", tags=["Stats"])
def get_stats(current: Annotated[TokenData, Depends(get_current_user_or_apikey)]):
    cid      = current.company_id
    products = db.all_products(cid)
    sales    = db.all_sales(cid)
    verif    = db.get_verification_stats(cid)
    return {
        "total_products":  len(products),
        "total_sales":     len(sales),
        "total_revenue":   sum(s.get("total", 0) for s in sales),
        "low_stock_count": len([p for p in products if p["stock"] < 10]),
        "low_stock_items": [p for p in products if p["stock"] < 10][:5],
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
