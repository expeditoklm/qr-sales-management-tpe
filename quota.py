"""
quota.py — Vérification des quotas par plan avant chaque création
"""
from fastapi import HTTPException
import database as db
from config import get_settings

cfg = get_settings()


def check_product_quota(company_id: str):
    """Lève HTTP 402 si le quota produits est atteint."""
    plan   = db.get_active_plan(company_id)
    limit  = cfg.PLAN_LIMITS[plan]["products"]
    current = db.count_products(company_id)
    if current >= limit:
        raise HTTPException(
            status_code=402,
            detail={
                "error":   "quota_exceeded",
                "message": f"Limite de {limit} produits atteinte pour le plan '{plan}'.",
                "plan":    plan,
                "limit":   limit,
                "current": current,
                "upgrade_hint": "Passez à un plan supérieur sur /billing/status",
            }
        )


def check_user_quota(company_id: str):
    """Lève HTTP 402 si le quota utilisateurs est atteint."""
    plan    = db.get_active_plan(company_id)
    limit   = cfg.PLAN_LIMITS[plan]["users"]
    current = db.count_users_for_company(company_id)
    if current >= limit:
        raise HTTPException(
            status_code=402,
            detail={
                "error":   "quota_exceeded",
                "message": f"Limite de {limit} utilisateurs atteinte pour le plan '{plan}'.",
                "plan":    plan,
                "limit":   limit,
                "current": current,
            }
        )


def check_transaction_quota(company_id: str):
    """Lève HTTP 402 si le quota de transactions mensuelles est atteint."""
    plan    = db.get_active_plan(company_id)
    limit   = cfg.PLAN_LIMITS[plan]["tx_month"]
    current = db.count_sales_this_month(company_id)
    if current >= limit:
        raise HTTPException(
            status_code=402,
            detail={
                "error":   "quota_exceeded",
                "message": f"Limite de {limit} transactions/mois atteinte pour le plan '{plan}'.",
                "plan":    plan,
                "limit":   limit,
                "current": current,
            }
        )


def check_subscription_active(company_id: str):
    """Lève HTTP 402 si l'abonnement est expiré (sauf plan free)."""
    sub = db.get_subscription(company_id)
    if not sub:
        return  # free tier implicite, pas de blocage
    if sub["status"] in ("canceled", "past_due", "unpaid"):
        raise HTTPException(
            status_code=402,
            detail={
                "error":   "subscription_expired",
                "message": "Abonnement expiré ou impayé. Renouvelez sur /billing/status.",
                "status":  sub["status"],
            }
        )