"""
storage.py — v5.0
Abstraction du stockage fichiers : local ou S3/Cloudflare R2.

Améliorations v5 :
  • Validation taille max (MAX_UPLOAD_SIZE_MB depuis config)
  • Validation type MIME strict (pas juste l'extension)
  • Retry automatique x3 sur les uploads S3 (réseau flaky)
  • Logs structurés pour chaque upload
  • Nettoyage des anciens fichiers locaux lors du remplacement
"""
import mimetypes
import shutil
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from config import get_settings

cfg = get_settings()

# ── Types autorisés ────────────────────────────────────────────────────────────
_ALLOWED_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".webp"}
_ALLOWED_MIME_TYPES  = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/octet-stream",  # fallback quand le MIME n'est pas détecté
}
_MAX_BYTES = int(getattr(cfg, "MAX_UPLOAD_SIZE_MB", 10)) * 1024 * 1024


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_extension(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in _ALLOWED_EXTENSIONS else ".jpg"


def _validate_upload(upload_file) -> None:
    """Lève HTTP 400 si le fichier est trop grand ou d'un type interdit."""
    # Taille
    upload_file.file.seek(0, 2)          # seek fin
    size = upload_file.file.tell()
    upload_file.file.seek(0)             # rembobiner
    if size > _MAX_BYTES:
        max_mb = _MAX_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Fichier trop volumineux. Maximum {max_mb} Mo autorisé.",
        )
    # Type MIME
    filename = getattr(upload_file, "filename", "") or ""
    ext = Path(filename).suffix.lower()
    if ext and ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Type de fichier non autorisé : {ext}. Utilisez JPG, PNG ou WebP.",
        )
    # L'extension ou le Content-Type HTTP viennent du client. Vérifier la
    # signature binaire évite d'héberger un contenu actif déguisé en image.
    header = upload_file.file.read(32)
    upload_file.file.seek(0)
    is_jpeg = header.startswith(b"\xff\xd8\xff")
    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_webp):
        raise HTTPException(
            status_code=415,
            detail="Le contenu du fichier n'est pas une image JPEG, PNG ou WebP valide.",
        )


def _s3_client():
    """Client boto3 mis en cache (paresseux pour ne pas bloquer le démarrage)."""
    if cfg.STORAGE_PROVIDER not in {"s3", "r2"}:
        return None
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 est requis pour STORAGE_PROVIDER=s3|r2. "
            "Lancez : pip install boto3"
        ) from exc
    if not cfg.STORAGE_BUCKET:
        raise RuntimeError(
            "STORAGE_BUCKET doit être configuré pour STORAGE_PROVIDER=s3|r2."
        )
    return boto3.client(
        "s3",
        region_name=cfg.STORAGE_REGION or None,
        endpoint_url=cfg.STORAGE_ENDPOINT_URL or None,
        aws_access_key_id=cfg.STORAGE_ACCESS_KEY_ID or None,
        aws_secret_access_key=cfg.STORAGE_SECRET_ACCESS_KEY or None,
    )


def _upload_to_s3(client, key: str, upload_file, ext: str) -> str:
    """Upload vers S3/R2 avec 3 tentatives."""
    content_type = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"
    last_exc = None
    for attempt in range(1, 4):
        try:
            upload_file.file.seek(0)
            client.upload_fileobj(
                upload_file.file,
                cfg.STORAGE_BUCKET,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=31536000",
                },
            )
            base_url = (cfg.STORAGE_PUBLIC_BASE_URL or "").rstrip("/")
            # Un même objet R2 peut être remplacé (logo ou photo produit).
            # Une version dans l'URL évite l'ancienne image conservée en cache.
            version = uuid.uuid4().hex
            url = f"{base_url}/{key}?v={version}" if base_url else f"{key}?v={version}"
            print(f"[Storage] S3 upload OK ({attempt}/3) : {key}")
            return url
        except Exception as exc:
            last_exc = exc
            print(f"[Storage] S3 tentative {attempt}/3 échouée : {exc}")
            if attempt < 3:
                time.sleep(0.5 * attempt)
    raise HTTPException(
        status_code=503,
        detail=f"Impossible d'uploader l'image vers le stockage cloud. Réessayez.",
    ) from last_exc


# ── API publique ───────────────────────────────────────────────────────────────

def save_product_image(company_id: str, product_id: str, upload_file, static_dir: Path) -> str:
    """Sauvegarde l'image d'un produit. Retourne l'URL publique."""
    _validate_upload(upload_file)
    ext = _safe_extension(getattr(upload_file, "filename", None))
    key = f"products/{company_id}/{product_id}{ext}"

    if cfg.STORAGE_PROVIDER in {"s3", "r2"}:
        client = _s3_client()
        return _upload_to_s3(client, key, upload_file, ext)

    # Stockage local
    img_dir = static_dir / "images" / company_id
    img_dir.mkdir(parents=True, exist_ok=True)
    # Supprimer l'ancienne image (toutes extensions)
    for old in img_dir.glob(f"{product_id}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    dest = img_dir / f"{product_id}{ext}"
    upload_file.file.seek(0)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    url = f"/static/images/{company_id}/{product_id}{ext}"
    print(f"[Storage] Local save : {dest}")
    return url


def save_company_logo(company_id: str, upload_file, static_dir: Path) -> str:
    """Sauvegarde le logo d'une boutique. Retourne l'URL publique."""
    _validate_upload(upload_file)
    ext = _safe_extension(getattr(upload_file, "filename", None))
    key = f"companies/{company_id}/logo{ext}"

    if cfg.STORAGE_PROVIDER in {"s3", "r2"}:
        client = _s3_client()
        return _upload_to_s3(client, key, upload_file, ext)

    # Stockage local
    img_dir = static_dir / "images" / company_id / "company"
    img_dir.mkdir(parents=True, exist_ok=True)
    # Supprimer l'ancien logo (toutes extensions)
    for old in img_dir.glob("logo.*"):
        try:
            old.unlink()
        except OSError:
            pass
    dest = img_dir / f"logo{ext}"
    upload_file.file.seek(0)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    url = f"/static/images/{company_id}/company/logo{ext}"
    print(f"[Storage] Local save : {dest}")
    return url


def delete_company_assets(company_id: str, static_dir: Path) -> None:
    """Supprime uniquement les médias appartenant à une boutique supprimée."""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_id)
    if not safe_id or safe_id != company_id:
        raise ValueError("Identifiant de boutique invalide pour la suppression des médias")

    if cfg.STORAGE_PROVIDER in {"s3", "r2"}:
        client = _s3_client()
        for prefix in (f"products/{safe_id}/", f"companies/{safe_id}/"):
            continuation = None
            while True:
                params = {"Bucket": cfg.STORAGE_BUCKET, "Prefix": prefix}
                if continuation:
                    params["ContinuationToken"] = continuation
                page = client.list_objects_v2(**params)
                objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if objects:
                    client.delete_objects(Bucket=cfg.STORAGE_BUCKET, Delete={"Objects": objects, "Quiet": True})
                if not page.get("IsTruncated"):
                    break
                continuation = page.get("NextContinuationToken")
        return

    images_root = (static_dir / "images").resolve()
    company_dir = (images_root / safe_id).resolve()
    if company_dir != images_root and images_root in company_dir.parents and company_dir.exists():
        shutil.rmtree(company_dir)
