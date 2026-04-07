"""
main.py — ERP + Authentification Produits v3.2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Compatibilité totale :
  • App Flutter  : tous champs anti-fraude (consumerCode, referenceImageHash…)
  • Webhook sale : /api/webhook/sale
  • Image produit: /api/products/{id}/image  (PATCH)
  • Client final : /api/verify  +  /api/public/verify
  • Dashboard web: servi sur /  et /verify sur /verify
"""

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from typing import List, Optional
from datetime import datetime
import uuid, random, string, httpx
from pathlib import Path

from fastapi import UploadFile, File
from fastapi.staticfiles import StaticFiles
import shutil


from models import (
    Product, ProductCreate, ProductUpdate,
    Sale, SaleCreate, SaleItem,
    StockUpdateRequest, WebhookPayload,
    ProductImageUpdate,
    AuthCode, GenerateCodesRequest,
    VerifyRequest, VerifyResponse,
)
import database as db
from auth import verify_api_key

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
STATIC_DIR = Path(__file__).parent / "static" / "images"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="ERP + Authentification Produits",
    description="Gestion stock, ventes & vérification d'authenticité client",
    version="3.2.0",
)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import FileResponse

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(FRONTEND_DIR / "favicon.ico")


# ════════════════════════════════════════════════════════════════════════════
# PAGES HTML  (résout le problème file:// CORS)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_dashboard():
    """Dashboard ERP → http://localhost:8000/"""
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))

@app.get("/verify",     response_class=HTMLResponse, include_in_schema=False)
@app.get("/verify.html",response_class=HTMLResponse, include_in_schema=False)
def page_verify():
    """Page client → http://localhost:8000/verify"""
    return HTMLResponse((FRONTEND_DIR / "verify.html").read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"])
def health():
    return {
        "status": "healthy",
        "version": "3.2.0",
        "timestamp": datetime.now().isoformat(),
        "verify_url": "http://localhost:8000/verify",
    }


# ════════════════════════════════════════════════════════════════════════════
# PRODUITS  (API Key requise)
# ════════════════════════════════════════════════════════════════════════════

def _blank(p: dict) -> dict:
    """S'assure que les clés optionnelles existent (pour INSERT)."""
    defaults = {
        "company_id": None, "image_url": None,
        "reference_image_url": None, "reference_image_hash": None,
        "consumer_code": None,
    }
    return {**defaults, **p}

@app.get("/api/products", response_model=List[Product], tags=["Produits"])
def list_products(api_key: str = Depends(verify_api_key)):
    return db.all_products()

@app.get("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def get_product(product_id: str, api_key: str = Depends(verify_api_key)):
    p = db.get_product(product_id)
    if not p: raise HTTPException(404, "Produit introuvable")
    return p

@app.post("/api/products", response_model=Product, status_code=201, tags=["Produits"])
def create_product(payload: ProductCreate, api_key: str = Depends(verify_api_key)):
    now = datetime.now().isoformat()
    pid = str(uuid.uuid4())
    product = _blank({
        "id": pid,
        "sku": payload.sku or f"SKU-{pid[:8].upper()}",
        "name": payload.name,
        "description": payload.description,
        "price": payload.price,
        "stock": payload.stock,
        "company_id":           payload.company_id,
        "image_url":            payload.image_url,
        "reference_image_url":  payload.reference_image_url,
        "reference_image_hash": payload.reference_image_hash,
        "consumer_code":        payload.consumer_code,
        "created_at": now,
        "updated_at": now,
    })
    db.insert_product(product)
    return product

@app.put("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def update_product(product_id: str, payload: ProductUpdate,
                   api_key: str = Depends(verify_api_key)):
    p = db.get_product(product_id)
    if not p: raise HTTPException(404, "Produit introuvable")
    p.update({k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None or k in payload.dict(exclude_unset=True)})
    p["updated_at"] = datetime.now().isoformat()
    db.update_product_full(_blank(p))
    return p

@app.delete("/api/products/{product_id}", tags=["Produits"])
def delete_product(product_id: str, api_key: str = Depends(verify_api_key)):
    if not db.get_product(product_id): raise HTTPException(404, "Produit introuvable")
    db.delete_product(product_id)
    return {"message": "Produit supprimé"}

@app.patch("/api/products/{product_id}/stock", response_model=Product, tags=["Produits"])
def update_stock(product_id: str, payload: StockUpdateRequest,
                 api_key: str = Depends(verify_api_key)):
    p = db.get_product(product_id)
    if not p: raise HTTPException(404, "Produit introuvable")
    new_stock = p["stock"] + payload.delta
    if new_stock < 0:
        raise HTTPException(400, f"Stock insuffisant (actuel: {p['stock']})")
    now = datetime.now().isoformat()
    db.update_stock(product_id, new_stock, now)
    p["stock"] = new_stock
    p["updated_at"] = now
    return p


# ── Image produit (appelé par Flutter ProductImageService) ───────────────────
@app.patch("/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
@app.post( "/api/products/{product_id}/image", response_model=Product, tags=["Produits"])

@app.patch("/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
@app.post( "/api/products/{product_id}/image", response_model=Product, tags=["Produits"])
async def update_product_image(
    product_id: str,
    request: Request,
    api_key: str = Depends(verify_api_key),
):
    p = db.get_product(product_id)
    if not p: raise HTTPException(404, "Produit introuvable")
    now = datetime.now().isoformat()

    content_type = request.headers.get("content-type", "")

    # ── CAS 1 : upload multipart/form-data (dashboard HTML) ──────────────
    if "multipart/form-data" in content_type:
        form = await request.form()
        file: UploadFile = form.get("file")
        if not file:
            raise HTTPException(400, "Champ 'file' manquant")

        # Détecter l'extension
        ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".jpg"

        filename = f"{product_id}{ext}"
        dest = STATIC_DIR / filename
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        image_url = f"/static/images/{filename}"
        db.update_product_image(
            product_id,
            image_url      = image_url,
            ref_image_url  = p.get("reference_image_url"),
            ref_image_hash = p.get("reference_image_hash"),
            updated_at     = now,
        )
        return db.get_product(product_id)

    # ── CAS 2 : JSON (Flutter ProductImageService) ────────────────────────
    body = await request.json()
    payload = ProductImageUpdate(**body)
    db.update_product_image(
        product_id,
        image_url      = payload.image_url      or p.get("image_url"),
        ref_image_url  = payload.reference_image_url  or p.get("reference_image_url"),
        ref_image_hash = payload.reference_image_hash or p.get("reference_image_hash"),
        updated_at     = now,
    )
    return db.get_product(product_id)

# ════════════════════════════════════════════════════════════════════════════
# VENTES  (API Key requise)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/sales", response_model=List[Sale], tags=["Ventes"])
def list_sales(api_key: str = Depends(verify_api_key)):
    return db.all_sales()

@app.get("/api/sales/{sale_id}", response_model=Sale, tags=["Ventes"])
def get_sale(sale_id: str, api_key: str = Depends(verify_api_key)):
    s = db.get_sale(sale_id)
    if not s: raise HTTPException(404, "Vente introuvable")
    return s

@app.post("/api/sales", response_model=Sale, status_code=201, tags=["Ventes"])
def create_sale(payload: SaleCreate, api_key: str = Depends(verify_api_key)):
    total, items_validated = 0, []
    for item in payload.items:
        p = db.get_product(item.product_id)
        if not p: raise HTTPException(404, f"Produit {item.product_id} introuvable")
        if p["stock"] < item.quantity:
            raise HTTPException(400, f"Stock insuffisant pour {p['name']}")
        sub = p["price"] * item.quantity
        total += sub
        items_validated.append({
            "product_id": item.product_id, "product_name": p["name"],
            "quantity": item.quantity, "unit_price": p["price"], "subtotal": sub,
        })
        db.update_stock(item.product_id, p["stock"] - item.quantity, datetime.now().isoformat())

    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    sale = {
        "id": sid,
        "reference": f"VTE-{datetime.now().strftime('%Y%m%d')}-{sid[:6].upper()}",
        "source": "dashboard",
        "items": items_validated, "total": total,
        "customer": payload.customer, "note": payload.note,
        "created_at": now,
    }
    db.insert_sale(sale)
    return sale


# ════════════════════════════════════════════════════════════════════════════
# WEBHOOK Flutter POS
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/webhook/sale", tags=["Webhook"])
def webhook_sale(payload: WebhookPayload, api_key: str = Depends(verify_api_key)):
    errors, processed, items_enriched = [], [], []
    total_calc = 0
    for item in payload.items:
        p = None
        if item.get("product_id"): p = db.get_product(item["product_id"])
        if not p and item.get("sku"): p = db.get_product_by_sku(item["sku"])
        qty = item.get("quantity", 1)
        if p:
            sub = p["price"] * qty
            total_calc += sub
            items_enriched.append({
                "product_id": p["id"], "product_name": p["name"],
                "sku": p.get("sku",""), "quantity": qty,
                "unit_price": p["price"], "subtotal": sub,
            })
            processed.append({"product_id": p["id"], "name": p["name"], "current_stock": p["stock"]})
        else:
            errors.append(f"Produit non trouvé: {item.get('product_id') or item.get('sku')}")
            items_enriched.append(item)

    sid = str(uuid.uuid4()); now = datetime.now().isoformat()
    db.insert_sale({
        "id": sid,
        "reference": payload.sale_reference or f"WH-{sid[:8].upper()}",
        "source": "flutter_pos", "items": items_enriched,
        "total": total_calc if total_calc > 0 else (payload.total or 0),
        "customer": None, "note": None, "created_at": now,
    })
    return {"success": len(errors)==0, "sale_id": sid,
            "processed": processed, "errors": errors, "timestamp": now}


# ════════════════════════════════════════════════════════════════════════════
# CODES D'AUTHENTIFICATION  (API Key requise)
# ════════════════════════════════════════════════════════════════════════════

def _make_code() -> str:
    """Code unique XXXXX-XXXXX (10 chiffres faciles à taper)."""
    while True:
        d = ''.join(random.choices(string.digits, k=10))
        code = f"{d[:5]}-{d[5:]}"
        if not db.get_auth_code_by_value(code):
            return code

@app.post("/api/products/{product_id}/codes/generate",
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"])
@app.post("/api/products/{product_id}/generate-codes",   # alias Flutter
          response_model=List[AuthCode], status_code=201, tags=["Codes Auth"],
          include_in_schema=False)
def generate_codes(product_id: str, payload: GenerateCodesRequest,
                   api_key: str = Depends(verify_api_key)):
    """Génère N codes d'authentification pour un produit (max 500/appel)."""
    if not db.get_product(product_id): raise HTTPException(404, "Produit introuvable")
    now = datetime.now().isoformat()
    created = []
    for _ in range(payload.quantity):
        entry = {"id": str(uuid.uuid4()), "product_id": product_id,
                 "code": _make_code(), "status": "active", "created_at": now}
        db.insert_auth_code(entry)
        created.append(entry)
    return created

@app.get("/api/products/{product_id}/codes",
         response_model=List[AuthCode], tags=["Codes Auth"])
def list_codes(product_id: str, api_key: str = Depends(verify_api_key)):
    if not db.get_product(product_id): raise HTTPException(404, "Produit introuvable")
    return db.get_codes_for_product(product_id)

@app.get("/api/products/{product_id}/verifications", tags=["Codes Auth"])
def product_verifications(product_id: str, api_key: str = Depends(verify_api_key)):
    if not db.get_product(product_id): raise HTTPException(404, "Produit introuvable")
    return db.get_verifications_for_product(product_id)


# ════════════════════════════════════════════════════════════════════════════
# VÉRIFICATION CLIENT FINAL  (PUBLIC — sans API Key)
# ════════════════════════════════════════════════════════════════════════════

async def _reverse_geocode(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json"},
                headers={"User-Agent": "ERP-AuthVerifier/3.2"},
            )
            if r.status_code == 200:
                addr = r.json().get("address", {})
                return {
                    "city": (addr.get("city") or addr.get("town") or
                             addr.get("village") or addr.get("county") or ""),
                    "country": addr.get("country", ""),
                }
    except Exception:
        pass
    return {"city": None, "country": None}


async def _do_verify(code_raw: str, latitude, longitude,
                     request: Request) -> dict:
    """
    Logique centrale partagée par /api/verify et /api/public/verify.
    Retourne un dict brut (compatible les deux formats).
    """
    code = code_raw.strip().upper()

    # ── Chercher par consumer_code (code numérique court imprimé sur emballage)
    auth_code = db.get_auth_code_by_value(code)

    # Si pas trouvé via auth_codes, chercher directement dans products.consumer_code
    product_from_consumer = None
    if not auth_code:
        product_from_consumer = db.get_product_by_consumer_code(code)
        if not product_from_consumer:
            return {
                "valid": False, "already_used": False,
                "product_name": None, "product_image": None, "product_image_url": None,
                "message": "Code invalide. Vérifiez la saisie ou contactez le vendeur.",
                "used_at": None,
            }

    # ── Via auth_codes (codes générés à la volée)
    if auth_code:
        product = db.get_product(auth_code["product_id"])
        if auth_code["status"] == "used":
            return {
                "valid": False, "already_used": True,
                "product_name":      product["name"] if product else None,
                "product_image":     product.get("image_url") if product else None,
                "product_image_url": product.get("image_url") if product else None,
                "message": "Ce code a déjà été utilisé. Si vous venez d'acheter ce produit, il est peut-être contrefait.",
                "used_at": None,
            }
        if not product: raise HTTPException(500, "Produit introuvable en base")
        code_id = auth_code["id"]
        product_id = auth_code["product_id"]
        # Marquer comme utilisé
        db.mark_code_used(code_id)
    else:
        # ── Via consumer_code direct dans le produit
        product = product_from_consumer
        code_id = None
        product_id = product["id"]

    # ── Géolocalisation
    city = country = None
    if latitude is not None and longitude is not None:
        geo = await _reverse_geocode(latitude, longitude)
        city, country = geo["city"], geo["country"]

    raw_ip = request.client.host if request.client else "unknown"
    anon_ip = ".".join(raw_ip.split(".")[:3] + ["xxx"]) if "." in raw_ip else raw_ip

    now = datetime.now().isoformat()

    # Enregistrer la vérification seulement si on a un code_id
    if code_id:
        db.insert_verification({
            "id": str(uuid.uuid4()),
            "code_id":    code_id,
            "product_id": product_id,
            "verified_at": now,
            "latitude": latitude, "longitude": longitude,
            "city": city, "country": country,
            "ip_address": anon_ip,
            "user_agent": request.headers.get("user-agent", "")[:200],
        })

    return {
        "valid": True,
        "already_used": False,
        "product_name":        product["name"],
        "product_description": product.get("description"),
        "product_image":       product.get("image_url"),      # format verify.html v1
        "product_image_url":   product.get("image_url"),      # format verify.html v2
        "message": f"Produit authentique — {product['name']}",
        "used_at": now,
        "verified_at": now,
    }


# ── /api/verify  — format attendu par le verify.html existant ────────────────
@app.post("/api/verify", tags=["Client Final"])
async def verify_v1(request: Request):
    """
    Endpoint public — format simplifié :
    { "code": "XXXXX-XXXXX", "latitude": …, "longitude": … }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")
    return await _do_verify(
        body.get("code", ""),
        body.get("latitude"),
        body.get("longitude"),
        request,
    )


# ── /api/public/verify — format Pydantic (nouveau dashboard) ─────────────────
@app.post("/api/public/verify", response_model=VerifyResponse, tags=["Client Final"])
async def verify_v2(payload: VerifyRequest, request: Request):
    lat = payload.latitude  if payload.consent else None
    lon = payload.longitude if payload.consent else None
    result = await _do_verify(payload.code, lat, lon, request)
    return VerifyResponse(
        valid=result["valid"],
        already_used=result["already_used"],
        product_name=result.get("product_name"),
        product_description=result.get("product_description"),
        product_image_url=result.get("product_image_url"),
        message=result["message"],
        verified_at=result.get("verified_at"),
    )


# ── Aperçu avant validation (public) ─────────────────────────────────────────
@app.get("/api/public/preview/{code}", tags=["Client Final"])
def preview_code(code: str):
    clean = code.strip().upper()
    ac = db.get_auth_code_by_value(clean)
    if ac:
        p = db.get_product(ac["product_id"])
        return {"product_name": p["name"] if p else None,
                "product_image_url": p.get("image_url") if p else None,
                "code_status": ac["status"]}
    p = db.get_product_by_consumer_code(clean)
    if p:
        return {"product_name": p["name"], "product_image_url": p.get("image_url"),
                "code_status": "active"}
    raise HTTPException(404, "Code introuvable")


# ════════════════════════════════════════════════════════════════════════════
# STATS  (API Key requise)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/stats", tags=["Stats"])
def get_stats(api_key: str = Depends(verify_api_key)):
    products = db.all_products()
    sales    = db.all_sales()
    verif    = db.get_verification_stats()
    return {
        "total_products":  len(products),
        "total_sales":     len(sales),
        "total_revenue":   sum(s.get("total", 0) for s in sales),
        "low_stock_count": len([p for p in products if p["stock"] < 10]),
        "low_stock_items": [p for p in products if p["stock"] < 10][:5],
        "verifications":   verif,
    }

@app.get("/api/stats/verifications", tags=["Stats"])
def verif_stats(api_key: str = Depends(verify_api_key)):
    return db.get_verification_stats()

@app.get("/api/verifications", tags=["Stats"])
def all_verifications_route(api_key: str = Depends(verify_api_key)):
    return db.all_verifications()

# Aliases utilisés dans certains frontends existants
@app.get("/api/authenticity/stats", tags=["Stats"], include_in_schema=False)
def auth_stats(api_key: str = Depends(verify_api_key)):
    with db.get_conn() as conn:
        total_codes = conn.execute("SELECT COUNT(*) FROM auth_codes").fetchone()[0]
        used_codes  = conn.execute("SELECT COUNT(*) FROM auth_codes WHERE status='used'").fetchone()[0]
        total_verif = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        # Tentatives suspectes = vérifications sur codes déjà utilisés (rejoués)
        fake_attempts = conn.execute("""
            SELECT COUNT(*) FROM verifications v
            JOIN auth_codes a ON v.code_id = a.id
            WHERE a.status = 'used'
              AND v.verified_at != (
                  SELECT MIN(verified_at) FROM verifications v2 WHERE v2.code_id = a.id
              )
        """).fetchone()[0]
    return {
        "total_codes":        total_codes,
        "used_codes":         used_codes,
        "total_verifications": total_verif,
        "fake_attempts":      fake_attempts,
    }
@app.get("/api/authenticity/logs", tags=["Stats"], include_in_schema=False)
def auth_logs(api_key: str = Depends(verify_api_key)):
    logs = db.all_verifications()
    # Enrichir chaque log avec la valeur textuelle du code
    for log in logs:
        code_id = log.get("code_id")
        if code_id:
            auth_code = db.get_auth_code_by_id(code_id)   # ← voir note ci-dessous
            log["code"] = auth_code["code"] if auth_code else code_id
        else:
            log["code"] = "—"
    return logs