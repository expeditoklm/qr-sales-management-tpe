"""
models.py — v4.0
Tous les modèles Pydantic : auth, billing, produits, ventes, codes auth
"""
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Any, Dict
import re


def _validate_email_format(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", normalized):
        raise ValueError("Adresse email invalide")
    return normalized


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=100)
    email:        str = Field(..., min_length=5)
    password:     str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)

    @field_validator("email")
    @classmethod
    def email_lower(cls, v: str) -> str:
        return _validate_email_format(v)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une majuscule")
        if not re.search(r"[0-9]", v):
            raise ValueError("Le mot de passe doit contenir au moins un chiffre")
        return v

    @field_validator("confirm_password")
    @classmethod
    def confirm_not_empty(cls, v: str) -> str:
        return v


class LoginRequest(BaseModel):
    identifier: Optional[str] = None
    email: Optional[str] = None
    password: str

    @field_validator("identifier", "email")
    @classmethod
    def login_value_normalized(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().lower()

    @model_validator(mode="after")
    def ensure_identifier(self):
        self.identifier = (self.identifier or self.email or "").strip().lower()
        if not self.identifier:
            raise ValueError("Le matricule ou l'email est requis")
        return self


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    user_id:       str
    company_id:    str
    company_name:  str
    secret_key:    str
    role:          str
    plan:          str


class RefreshRequest(BaseModel):
    refresh_token: str


class InviteRequest(BaseModel):
    email: str
    role:  str = "employee"  # "employee" | "manager" | "admin"

    @field_validator("email")
    @classmethod
    def email_lower(cls, v: str) -> str:
        return _validate_email_format(v)

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("employee", "manager", "admin"):
            raise ValueError("Role doit être 'employee', 'manager' ou 'admin'")
        return v


class AcceptInviteRequest(BaseModel):
    token:    str
    password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une majuscule")
        if not re.search(r"[0-9]", v):
            raise ValueError("Le mot de passe doit contenir au moins un chiffre")
        return v


class UserOut(BaseModel):
    id:         str
    company_id: str
    email:      str
    role:       str
    is_active:  int
    created_at: str
    email_verified: int = 0


class ActionResponse(BaseModel):
    success: bool = True
    message: str
    preview: Optional[Dict[str, Any]] = None


class EmailVerificationRequest(BaseModel):
    token: str


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def forgot_email_lower(cls, v: str) -> str:
        return _validate_email_format(v)


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)


class AdminResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)


# ═══════════════════════════════════════════════════════════════════════════════
# BILLING
# ═══════════════════════════════════════════════════════════════════════════════

class SubscriptionStatus(BaseModel):
    company_id: str
    plan:       str
    status:     str
    start_date: Optional[str] = None
    end_date:   Optional[str] = None
    limits:     Dict[str, int] = {}


class CreateCheckoutRequest(BaseModel):
    plan:         str = Field(..., description="basic | pro | enterprise")
    success_url:  str
    cancel_url:   str

    @field_validator("plan")
    @classmethod
    def valid_plan(cls, v: str) -> str:
        if v not in ("basic", "pro", "enterprise"):
            raise ValueError("Plan invalide")
        return v


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUITS
# ═══════════════════════════════════════════════════════════════════════════════

class ProductCreate(BaseModel):
    name:        str
    price:       float
    stock:       int
    sku:         Optional[str]   = None
    description: Optional[str]  = None
    # Champs anti-fraude Flutter
    image_url:             Optional[str] = None
    reference_image_url:   Optional[str] = None
    reference_image_hash:  Optional[str] = None
    consumer_code:         Optional[str] = None


class ProductUpdate(BaseModel):
    name:                  Optional[str]   = None
    price:                 Optional[float] = None
    stock:                 Optional[int]   = None
    sku:                   Optional[str]   = None
    description:           Optional[str]   = None
    image_url:             Optional[str]   = None
    reference_image_url:   Optional[str]   = None
    reference_image_hash:  Optional[str]   = None
    consumer_code:         Optional[str]   = None


class Product(BaseModel):
    id:                    str
    sku:                   str
    name:                  str
    description:           Optional[str]   = None
    price:                 float
    stock:                 int
    image_url:             Optional[str]   = None
    reference_image_url:   Optional[str]   = None
    reference_image_hash:  Optional[str]   = None
    consumer_code:         Optional[str]   = None
    created_at:            str
    updated_at:            str
    # Champ legacy pour compat Flutter (retourné mais pas stocké dans tenant DB)
    company_id:            Optional[str]   = None


class StockUpdateRequest(BaseModel):
    delta:  int = Field(..., description="Positif = ajout, négatif = décrément")
    reason: Optional[str] = "manual"


class ProductImageUpdate(BaseModel):
    image_url:             Optional[str] = None
    reference_image_url:   Optional[str] = None
    reference_image_hash:  Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# VENTES
# ═══════════════════════════════════════════════════════════════════════════════

class SaleItem(BaseModel):
    product_id: str
    quantity:   int


class SaleCreate(BaseModel):
    items:    List[SaleItem]
    customer: Optional[str] = None
    note:     Optional[str] = None
    source:   Optional[str] = "dashboard"


class Sale(BaseModel):
    id:        str
    reference: str
    items:     List[Any]
    total:     float
    customer:  Optional[str] = None
    note:      Optional[str] = None
    created_at: str


class WebhookPayload(BaseModel):
    sale_reference: Optional[str]          = None
    total:          Optional[float]         = 0
    items:          List[Dict[str, Any]]
    source:         Optional[str]           = "flutter_pos"
    company_id:     Optional[str]           = None


# ═══════════════════════════════════════════════════════════════════════════════
# CODES D'AUTHENTICITÉ
# ═══════════════════════════════════════════════════════════════════════════════

class AuthCode(BaseModel):
    id:         str
    product_id: str
    code:       str
    status:     str
    created_at: str
    verified_at: Optional[str]   = None
    latitude:    Optional[float] = None
    longitude:   Optional[float] = None
    city:        Optional[str]   = None
    country:     Optional[str]   = None


class GenerateCodesRequest(BaseModel):
    quantity: int = Field(default=1, ge=1, le=500)


# ═══════════════════════════════════════════════════════════════════════════════
# VÉRIFICATION CLIENT FINAL
# ═══════════════════════════════════════════════════════════════════════════════

class VerifyRequest(BaseModel):
    code:      str
    latitude:  Optional[float] = None
    longitude: Optional[float] = None
    consent:   bool = Field(default=False)


class VerifyResponse(BaseModel):
    valid:               bool
    already_used:        bool          = False
    fraud_attempt:       bool          = False
    product_name:        Optional[str] = None
    product_description: Optional[str] = None
    product_image_url:   Optional[str] = None
    product_image:       Optional[str] = None  # alias legacy
    company_name:        Optional[str] = None
    company_email:       Optional[str] = None
    company_status:      Optional[str] = None
    verification_count:  int = 0
    fraud_attempts:      int = 0
    location_consent:    bool = False
    message:             str
    verified_at:         Optional[str] = None
    used_at:             Optional[str] = None  # alias legacy


# ═══════════════════════════════════════════════════════════════════════════════
# SUPER-ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

class CompanyOut(BaseModel):
    id:         str
    name:       str
    email:      str
    plan:       str
    status:     str
    created_at: str
    sub_plan:    Optional[str] = None
    sub_status:  Optional[str] = None
    subscription_start_date: Optional[str] = None
    subscription_end_date: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
