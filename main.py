from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from datetime import datetime
import uuid

from models import (
    Product, ProductCreate, ProductUpdate,
    Sale, SaleCreate, SaleItem,
    StockUpdateRequest, WebhookPayload
)
import database as db
from auth import verify_api_key

app = FastAPI(
    title="ERP Système Existant",
    description="API de gestion stock & ventes — SQLite persistant",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────
@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "service": "ERP SQLite", "version": "2.0.0"}

@app.get("/health", tags=["Health"])
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


# ─────────────────────────────────────────────
# PRODUITS
# ─────────────────────────────────────────────
@app.get("/api/products", response_model=List[Product], tags=["Produits"])
def list_products(api_key: str = Depends(verify_api_key)):
    return db.all_products()

@app.get("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def get_product(product_id: str, api_key: str = Depends(verify_api_key)):
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return product

@app.post("/api/products", response_model=Product, status_code=201, tags=["Produits"])
def create_product(payload: ProductCreate, api_key: str = Depends(verify_api_key)):
    now = datetime.now().isoformat()
    product_id = str(uuid.uuid4())
    product = {
        "id": product_id,
        "sku": payload.sku or f"SKU-{product_id[:8].upper()}",
        "name": payload.name,
        "description": payload.description,
        "price": payload.price,
        "stock": payload.stock,
        "created_at": now,
        "updated_at": now,
    }
    db.insert_product(product)
    return product

@app.put("/api/products/{product_id}", response_model=Product, tags=["Produits"])
def update_product(product_id: str, payload: ProductUpdate, api_key: str = Depends(verify_api_key)):
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    update_data = payload.dict(exclude_unset=True)
    product.update(update_data)
    product["updated_at"] = datetime.now().isoformat()
    db.update_product_full(product)
    return product

@app.delete("/api/products/{product_id}", tags=["Produits"])
def delete_product(product_id: str, api_key: str = Depends(verify_api_key)):
    if not db.get_product(product_id):
        raise HTTPException(status_code=404, detail="Produit introuvable")
    db.delete_product(product_id)
    return {"message": "Produit supprimé"}

@app.patch("/api/products/{product_id}/stock", response_model=Product, tags=["Produits"])
def update_stock(product_id: str, payload: StockUpdateRequest, api_key: str = Depends(verify_api_key)):
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    new_stock = product["stock"] + payload.delta
    if new_stock < 0:
        raise HTTPException(status_code=400, detail=f"Stock insuffisant (actuel: {product['stock']})")
    now = datetime.now().isoformat()
    db.update_stock(product_id, new_stock, now)
    product["stock"] = new_stock
    product["updated_at"] = now
    return product


# ─────────────────────────────────────────────
# VENTES
# ─────────────────────────────────────────────
@app.get("/api/sales", response_model=List[Sale], tags=["Ventes"])
def list_sales(api_key: str = Depends(verify_api_key)):
    return db.all_sales()

@app.get("/api/sales/{sale_id}", response_model=Sale, tags=["Ventes"])
def get_sale(sale_id: str, api_key: str = Depends(verify_api_key)):
    sale = db.get_sale(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Vente introuvable")
    return sale

@app.post("/api/sales", response_model=Sale, status_code=201, tags=["Ventes"])
def create_sale(payload: SaleCreate, api_key: str = Depends(verify_api_key)):
    total = 0
    items_validated = []
    for item in payload.items:
        product = db.get_product(item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"Produit {item.product_id} introuvable")
        if product["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Stock insuffisant pour {product['name']}")
        subtotal = product["price"] * item.quantity
        total += subtotal
        items_validated.append({
            "product_id": item.product_id,
            "product_name": product["name"],
            "quantity": item.quantity,
            "unit_price": product["price"],
            "subtotal": subtotal
        })
        new_stock = product["stock"] - item.quantity
        db.update_stock(item.product_id, new_stock, datetime.now().isoformat())

    sale_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    sale = {
        "id": sale_id,
        "reference": f"VTE-{datetime.now().strftime('%Y%m%d')}-{sale_id[:6].upper()}",
        "source": "dashboard",
        "items": items_validated,
        "total": total,
        "customer": payload.customer,
        "note": payload.note,
        "created_at": now,
    }
    db.insert_sale(sale)
    return sale


# ─────────────────────────────────────────────
# WEBHOOK (reçoit les ventes Flutter)
# ─────────────────────────────────────────────
@app.post("/api/webhook/sale", tags=["Webhook"])
def webhook_sale(payload: WebhookPayload, api_key: str = Depends(verify_api_key)):
    """
    Reçoit les ventes depuis l'app Flutter.
    NE décrémente PAS le stock (déjà fait par Flutter via PATCH /stock).
    Enregistre la vente avec les détails enrichis.
    """
    errors = []
    processed = []
    items_enriched = []
    total_calculated = 0

    for item in payload.items:
        product = None
        if item.get("product_id"):
            product = db.get_product(item["product_id"])
        if not product and item.get("sku"):
            product = db.get_product_by_sku(item["sku"])

        qty = item.get("quantity", 1)
        if product:
            subtotal = product["price"] * qty
            total_calculated += subtotal
            items_enriched.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "sku": product.get("sku", ""),
                "quantity": qty,
                "unit_price": product["price"],
                "subtotal": subtotal,
            })
            processed.append({
                "product_id": product["id"],
                "name": product["name"],
                "current_stock": product["stock"]
            })
        else:
            errors.append(f"Produit non trouvé: {item.get('product_id') or item.get('sku')}")
            items_enriched.append(item)

    total_final = total_calculated if total_calculated > 0 else (payload.total or 0)
    sale_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    sale = {
        "id": sale_id,
        "reference": payload.sale_reference or f"WH-{sale_id[:8].upper()}",
        "source": "flutter_pos",
        "items": items_enriched,
        "total": total_final,
        "customer": None,
        "note": None,
        "created_at": now,
    }
    db.insert_sale(sale)

    return {
        "success": len(errors) == 0,
        "sale_id": sale_id,
        "processed": processed,
        "errors": errors,
        "timestamp": now,
    }


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────
@app.get("/api/stats", tags=["Stats"])
def get_stats(api_key: str = Depends(verify_api_key)):
    products = db.all_products()
    sales = db.all_sales()
    total_revenue = sum(s.get("total", 0) for s in sales)
    low_stock = [p for p in products if p["stock"] < 10]
    return {
        "total_products": len(products),
        "total_sales": len(sales),
        "total_revenue": total_revenue,
        "low_stock_count": len(low_stock),
        "low_stock_items": low_stock[:5],
    }
