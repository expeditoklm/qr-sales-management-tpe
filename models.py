"""
models.py — v3.2
Modèles Pydantic compatibles Flutter (consumerCode, referenceImageHash, companyId…)
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict


# ── Produits ─────────────────────────────────────────────────────────────────
class ProductCreate(BaseModel):
    name: str
    price: float
    stock: int
    sku: Optional[str] = None
    description: Optional[str] = None
    # Champs Flutter anti-fraude
    company_id:            Optional[str] = None
    image_url:             Optional[str] = None   # image visible boutique
    reference_image_url:   Optional[str] = None   # image de référence anti-fraude
    reference_image_hash:  Optional[str] = None   # sha256 de l'image de référence
    consumer_code:         Optional[str] = None   # code à saisir par le client final


class ProductUpdate(BaseModel):
    name:                  Optional[str]   = None
    price:                 Optional[float] = None
    stock:                 Optional[int]   = None
    sku:                   Optional[str]   = None
    description:           Optional[str]   = None
    company_id:            Optional[str]   = None
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
    company_id:            Optional[str]   = None
    image_url:             Optional[str]   = None
    reference_image_url:   Optional[str]   = None
    reference_image_hash:  Optional[str]   = None
    consumer_code:         Optional[str]   = None
    created_at:            str
    updated_at:            str


class StockUpdateRequest(BaseModel):
    delta:  int = Field(..., description="Positif = ajout, négatif = décrément")
    reason: Optional[str] = "manual"


# ── Ventes ───────────────────────────────────────────────────────────────────
class SaleItem(BaseModel):
    product_id: str
    quantity:   int


class SaleCreate(BaseModel):
    items:    List[SaleItem]
    customer: Optional[str] = None
    note:     Optional[str] = None


class Sale(BaseModel):
    id:        str
    reference: str
    items:     List[Any]
    total:     float
    customer:  Optional[str] = None
    note:      Optional[str] = None
    created_at: str


class WebhookPayload(BaseModel):
    sale_reference: Optional[str]            = None
    total:          Optional[float]           = 0
    items:          List[Dict[str, Any]]
    source:         Optional[str]             = "flutter_pos"
    company_id:     Optional[str]             = None


# ── Image upload produit ─────────────────────────────────────────────────────
class ProductImageUpdate(BaseModel):
    """Envoyé par Flutter lors de la sauvegarde d'une image de référence."""
    image_url:             Optional[str] = None
    reference_image_url:   Optional[str] = None
    reference_image_hash:  Optional[str] = None


# ── Codes d'authentification ─────────────────────────────────────────────────
class AuthCode(BaseModel):
    id:         str
    product_id: str
    code:       str
    status:     str
    created_at: str
    # Champs joints depuis verifications
    verified_at: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city: Optional[str] = None
    country: Optional[str] = None


class GenerateCodesRequest(BaseModel):
    quantity: int = Field(default=1, ge=1, le=500,
                          description="Nombre de codes à générer (max 500)")


# ── Vérification client final ─────────────────────────────────────────────────
class VerifyRequest(BaseModel):
    code:      str
    latitude:  Optional[float] = None
    longitude: Optional[float] = None
    consent:   bool = Field(default=False,
                            description="Consentement collecte géolocalisation")


class VerifyResponse(BaseModel):
    valid:                 bool
    already_used:          bool           = False
    product_name:          Optional[str]  = None
    product_description:   Optional[str]  = None
    product_image_url:     Optional[str]  = None
    message:               str
    verified_at:           Optional[str]  = None


class Verification(BaseModel):
    id:          str
    code_id:     str
    product_id:  str
    verified_at: str
    latitude:    Optional[float] = None
    longitude:   Optional[float] = None
    city:        Optional[str]   = None
    country:     Optional[str]   = None
