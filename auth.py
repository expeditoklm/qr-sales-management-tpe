from fastapi import HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

API_KEY = "erp-secret-key-2024"  # À mettre dans .env en prod
API_KEY_NAME = "X-API-Key"

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="API Key invalide ou manquante. Fournir X-API-Key dans les headers."
        )
    return api_key
