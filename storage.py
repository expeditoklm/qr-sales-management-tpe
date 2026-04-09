import mimetypes
import shutil
from functools import lru_cache
from pathlib import Path

from config import get_settings

cfg = get_settings()

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _safe_extension(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in _ALLOWED_EXTENSIONS else ".jpg"


@lru_cache(maxsize=1)
def _s3_client():
    if cfg.STORAGE_PROVIDER not in {"s3", "r2"}:
        return None
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 est requis pour STORAGE_PROVIDER=s3|r2"
        ) from exc
    return boto3.client(
        "s3",
        region_name=cfg.STORAGE_REGION or None,
        endpoint_url=cfg.STORAGE_ENDPOINT_URL or None,
        aws_access_key_id=cfg.STORAGE_ACCESS_KEY_ID or None,
        aws_secret_access_key=cfg.STORAGE_SECRET_ACCESS_KEY or None,
    )


def save_product_image(company_id: str, product_id: str, upload_file, static_dir: Path) -> str:
    ext = _safe_extension(getattr(upload_file, "filename", None))
    key = f"products/{company_id}/{product_id}{ext}"

    if cfg.STORAGE_PROVIDER in {"s3", "r2"} and cfg.STORAGE_BUCKET:
        content_type = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"
        client = _s3_client()
        assert client is not None
        upload_file.file.seek(0)
        client.upload_fileobj(
            upload_file.file,
            cfg.STORAGE_BUCKET,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        base_url = (cfg.STORAGE_PUBLIC_BASE_URL or "").rstrip("/")
        if base_url:
            return f"{base_url}/{key}"
        return key

    img_dir = static_dir / "images" / company_id
    img_dir.mkdir(parents=True, exist_ok=True)
    dest = img_dir / f"{product_id}{ext}"
    upload_file.file.seek(0)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    return f"/static/images/{company_id}/{product_id}{ext}"
