from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict


class ProductCreate(BaseModel):
    name: str
    price: float
    stock: int
    sku: Optional[str] = None
    description: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    sku: Optional[str] = None
    description: Optional[str] = None


class Product(BaseModel):
    id: str
    sku: str
    name: str
    description: Optional[str] = None
    price: float
    stock: int
    created_at: str
    updated_at: str


class StockUpdateRequest(BaseModel):
    delta: int = Field(..., description="Positif = ajout, négatif = décrément")
    reason: Optional[str] = "manual"


class SaleItem(BaseModel):
    product_id: str
    quantity: int


class SaleCreate(BaseModel):
    items: List[SaleItem]
    customer: Optional[str] = None
    note: Optional[str] = None


class Sale(BaseModel):
    id: str
    reference: str
    items: List[Any]
    total: float
    customer: Optional[str] = None
    note: Optional[str] = None
    created_at: str


class WebhookPayload(BaseModel):
    sale_reference: Optional[str] = None
    total: Optional[float] = 0
    items: List[Dict[str, Any]]
    source: Optional[str] = "flutter_pos"
    company_id: Optional[str] = None
