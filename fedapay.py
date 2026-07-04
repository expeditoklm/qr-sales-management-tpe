"""
fedapay.py -- Client FedaPay pour QuickSellPay
Passerelle de paiement West Africa : MTN Mobile Money, Moov Money, cartes

Documentation : https://docs.fedapay.com
Sandbox : https://sandbox-api.fedapay.com/v1
Production : https://api.fedapay.com/v1

Flux d'un abonnement :
  1. POST /billing/fedapay/checkout  -> cree une transaction + retourne URL paiement
  2. Client paie sur la page FedaPay (MTN, Moov, carte)
  3. FedaPay appelle POST /billing/fedapay/webhook
  4. On met a jour le plan de la boutique
"""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import HTTPException

from config import get_settings

cfg = get_settings()


# ============================================================
# Configuration
# ============================================================

def _base_url() -> str:
    if getattr(cfg, "FEDAPAY_ENV", "sandbox") == "live":
        return "https://api.fedapay.com/v1"
    return "https://sandbox-api.fedapay.com/v1"


def _headers() -> dict:
    key = getattr(cfg, "FEDAPAY_SECRET_KEY", "")
    if not key:
        raise HTTPException(
            501,
            "FEDAPAY_SECRET_KEY non configure dans .env -- "
            "Creer un compte sur https://fedapay.com"
        )
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


# ============================================================
# Tarifs par plan (en XOF -- Franc CFA)
# ============================================================

PLAN_PRICES_XOF: dict[str, int] = {
    "basic":      getattr(cfg, "FEDAPAY_PRICE_BASIC",      5000),
    "pro":        getattr(cfg, "FEDAPAY_PRICE_PRO",       15000),
    "enterprise": getattr(cfg, "FEDAPAY_PRICE_ENTERPRISE", 30000),
}

PLAN_LABELS: dict[str, str] = {
    "basic":      "QuickSellPay BASIC -- 1 mois",
    "pro":        "QuickSellPay PRO -- 1 mois",
    "enterprise": "QuickSellPay ENTERPRISE -- 1 mois",
}


# ============================================================
# Client HTTP
# ============================================================

def create_transaction(
    plan: str,
    company_id: str,
    customer_email: str,
    customer_name: str,
    callback_url: str,
    cancel_url: str,
) -> dict:
    """
    Cree une transaction FedaPay et retourne l'URL de paiement.

    Retourne :
        {
            "transaction_id": "tr_xxx",
            "payment_url":    "https://checkout.fedapay.com/...",
            "amount":         5000,
            "currency":       "XOF"
        }
    """
    amount = PLAN_PRICES_XOF.get(plan)
    if not amount:
        raise HTTPException(400, f"Plan inconnu : {plan}. Choisir basic, pro ou enterprise.")

    body = {
        "description": PLAN_LABELS.get(plan, f"QuickSellPay {plan}"),
        "amount":      amount,
        "currency":    {"iso": "XOF"},
        "callback_url": callback_url,
        "cancel_url":   cancel_url,
        "customer": {
            "email":     customer_email,
            "firstname": customer_name.split()[0] if customer_name else "Client",
            "lastname":  " ".join(customer_name.split()[1:]) if len(customer_name.split()) > 1 else "",
        },
        "metadata": {
            "company_id": company_id,
            "plan":       plan,
        },
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{_base_url()}/transactions",
                headers=_headers(),
                json=body,
            )
    except httpx.TimeoutException:
        raise HTTPException(503, "FedaPay ne repond pas. Reessayez dans un moment.")
    except httpx.RequestError as e:
        raise HTTPException(503, f"Impossible de joindre FedaPay : {e}")

    if resp.status_code not in (200, 201):
        detail = _extract_error(resp)
        raise HTTPException(resp.status_code, f"FedaPay erreur : {detail}")

    data = resp.json()
    print(f"[FedaPay] create response keys: {list(data.keys())}")
    transaction = data.get("v1/transaction") or data.get("transaction") or data
    tx_id_raw = transaction.get("id")
    if not tx_id_raw:
        print(f"[FedaPay] ERREUR: id absent dans {transaction}")
        raise HTTPException(500, "FedaPay n'a pas retourne d'ID de transaction")
    transaction_id = str(tx_id_raw)
    print(f"[FedaPay] transaction_id={transaction_id}")

    # Obtenir le token de paiement (URL de checkout)
    payment_url = _get_payment_url(transaction_id)

    return {
        "transaction_id": transaction_id,
        "payment_url":    payment_url,
        "amount":         amount,
        "currency":       "XOF",
        "plan":           plan,
    }


def _get_payment_url(transaction_id: str) -> str:
    """
    Appelle POST /transactions/{id}/token pour obtenir l'URL de checkout FedaPay.
    FedaPay retourne l'URL directement (url / payment_url) ou un token JWT.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{_base_url()}/transactions/{transaction_id}/token",
                headers=_headers(),
            )
    except Exception as e:
        raise HTTPException(503, f"Impossible d'obtenir l'URL FedaPay : {e}")

    if resp.status_code not in (200, 201):
        raise HTTPException(resp.status_code, f"FedaPay token error : {_extract_error(resp)}")

    data = resp.json()
    print(f"[FedaPay] token response: {data}")

    # FedaPay peut encapsuler sous v1/token
    inner = data.get("v1/token") or data

    # Essayer l'URL directe d'abord (plus fiable)
    direct_url = (
        inner.get("url")
        or inner.get("payment_url")
        or inner.get("checkout_url")
        or data.get("url")
        or data.get("payment_url")
    )
    if direct_url:
        print(f"[FedaPay] checkout URL (native): {direct_url}")
        return direct_url

    # Fallback : construire depuis le token JWT
    token = inner.get("token") or data.get("token")
    if not token:
        print(f"[FedaPay] reponse inattendue: {data}")
        raise HTTPException(500, "FedaPay n'a pas retourne de token de paiement")

    env = getattr(cfg, "FEDAPAY_ENV", "sandbox")
    base = "https://checkout.fedapay.com" if env == "live" else "https://sandbox-checkout.fedapay.com"
    url = f"{base}/pay/{token}"
    print(f"[FedaPay] checkout URL (construite): {url}")
    return url


# ============================================================
# Verification du webhook
# ============================================================

def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    """
    Verifie la signature HMAC-SHA256 envoyee par FedaPay dans
    le header X-Fedapay-Signature.
    Retourne True si valide, False sinon.
    """
    secret = getattr(cfg, "FEDAPAY_WEBHOOK_SECRET", "")
    if not secret:
        # Pas de secret configure : accepter (dev uniquement)
        return True
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_webhook_event(body: dict) -> dict | None:
    """
    Parse l'evenement webhook FedaPay et retourne les infos utiles.
    Retourne None si l'evenement n'est pas pertinent.

    Format FedaPay webhook :
        {
            "name": "transaction.approved" | "transaction.declined" | ...,
            "object": { "id": ..., "status": ..., "metadata": {...} }
        }
    """
    event_name = body.get("name", "")
    obj        = body.get("object") or body.get("v1/transaction") or {}

    status   = str(obj.get("status", "")).lower()
    metadata = obj.get("metadata") or {}

    company_id = str(metadata.get("company_id") or "")
    plan       = str(metadata.get("plan") or "basic")
    tx_id      = str(obj.get("id") or "")

    if not company_id or not tx_id:
        return None

    # Evenements qui activent l'abonnement
    if event_name in ("transaction.approved",) or status in ("approved", "success"):
        return {
            "event":      "approved",
            "company_id": company_id,
            "plan":       plan,
            "tx_id":      tx_id,
        }

    # Echec de paiement
    if event_name in ("transaction.declined", "transaction.canceled") or \
       status in ("declined", "canceled", "refunded"):
        return {
            "event":      "declined",
            "company_id": company_id,
            "plan":       plan,
            "tx_id":      tx_id,
        }

    return None


# ============================================================
# Helpers
# ============================================================

def _extract_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return (
            data.get("message")
            or data.get("error")
            or data.get("detail")
            or str(data)
        )
    except Exception:
        return resp.text[:200]


def subscription_end_date(days: int = 31) -> str:
    """Retourne la date de fin d'abonnement (dans N jours) en ISO 8601."""
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()
