"""
backup.py -- Sauvegarde automatique QuickSellPay
================================================
Sauvegarde la base MySQL + les images uploadees dans un .tar.gz date.
Compatible cPanel cron (pas de systemd, pas de Docker).

Usage manuel :
    python backup.py                  # sauvegarde complete
    python backup.py --mysql-only     # base de donnees seulement
    python backup.py --images-only    # images seulement
    python backup.py --keep 7         # garder 7 jours de backups (defaut: 14)
    python backup.py --dry-run        # simuler sans ecrire

Ajouter au cron cPanel (tous les jours a 2h00) :
    0 2 * * * /home/tunelaf/quicksellpay/venv/bin/python /home/tunelaf/quicksellpay/backup.py >> /home/tunelaf/quicksellpay/logs/backup.log 2>&1
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# -- Chemin du projet
BASE_DIR = Path(__file__).parent

# Charger .env sans importer config (pour pouvoir utiliser ce script standalone)
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# -- Configuration depuis l'environnement
MYSQL_HOST     = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT     = os.environ.get("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "quicksellpay")
MYSQL_USER     = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
DB_ENGINE      = os.environ.get("DB_ENGINE", "sqlite").lower()

BACKUP_DIR     = Path(os.environ.get("BACKUP_DIR", str(BASE_DIR / "backups")))
STATIC_DIR     = BASE_DIR / "static" / "images"
DATA_DIR       = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))

DEFAULT_KEEP_DAYS = 14


# ============================================================
# Helpers
# ============================================================

def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def human_size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ============================================================
# Sauvegarde MySQL
# ============================================================

def backup_mysql(dest_dir: Path, dry_run: bool = False) -> Path | None:
    """
    Dump MySQL via mysqldump vers un fichier .sql.gz.
    Retourne le chemin du fichier cree, ou None en cas d echec.
    """
    filename = dest_dir / f"mysql_{MYSQL_DATABASE}_{stamp()}.sql.gz"
    log(f"MySQL dump -> {filename.name}")

    if dry_run:
        log("  [dry-run] skip")
        return filename

    # Construire la commande mysqldump
    cmd = [
        "mysqldump",
        f"--host={MYSQL_HOST}",
        f"--port={MYSQL_PORT}",
        f"--user={MYSQL_USER}",
        "--single-transaction",      # dump coherent sans locker les tables
        "--routines",
        "--events",
        "--set-gtid-purged=OFF",     # evite erreur si GTID non active
        MYSQL_DATABASE,
    ]
    env = os.environ.copy()
    if MYSQL_PASSWORD:
        env["MYSQL_PWD"] = MYSQL_PASSWORD  # eviter le mot de passe en clair dans cmd

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            timeout=300,  # 5 minutes max
        )
    except FileNotFoundError:
        log("  ERREUR : mysqldump introuvable. Installer mysql-client ou verifier le PATH.")
        return None
    except subprocess.TimeoutExpired:
        log("  ERREUR : mysqldump timeout (> 5 min). Base trop grosse ?")
        return None

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")[:500]
        log(f"  ERREUR mysqldump (code {proc.returncode}) : {stderr}")
        return None

    # Compresser le dump
    with gzip.open(filename, "wb") as gz:
        gz.write(proc.stdout)

    log(f"  OK  {human_size(filename)}")
    return filename


def backup_sqlite(dest_dir: Path, dry_run: bool = False) -> list[Path]:
    """
    Copie les fichiers .db SQLite dans un tar.gz (mode dev).
    """
    if not DATA_DIR.exists():
        log("  DATA_DIR introuvable, pas de backup SQLite")
        return []

    db_files = list(DATA_DIR.glob("**/*.db"))
    if not db_files:
        log("  Aucun fichier .db trouve dans DATA_DIR")
        return []

    filename = dest_dir / f"sqlite_{stamp()}.tar.gz"
    log(f"SQLite backup ({len(db_files)} fichiers) -> {filename.name}")

    if dry_run:
        log("  [dry-run] skip")
        return [filename]

    with tarfile.open(filename, "w:gz") as tar:
        for db in db_files:
            tar.add(db, arcname=db.relative_to(BASE_DIR))

    log(f"  OK  {human_size(filename)}")
    return [filename]


# ============================================================
# Sauvegarde des images
# ============================================================

def backup_images(dest_dir: Path, dry_run: bool = False) -> Path | None:
    """
    Compresse le dossier static/images/ en .tar.gz.
    """
    if not STATIC_DIR.exists():
        log("  static/images/ introuvable, skip")
        return None

    # Compter les fichiers
    files = [f for f in STATIC_DIR.rglob("*") if f.is_file()]
    if not files:
        log("  static/images/ vide, skip")
        return None

    filename = dest_dir / f"images_{stamp()}.tar.gz"
    log(f"Images ({len(files)} fichiers) -> {filename.name}")

    if dry_run:
        log("  [dry-run] skip")
        return filename

    with tarfile.open(filename, "w:gz") as tar:
        tar.add(STATIC_DIR, arcname="static/images")

    log(f"  OK  {human_size(filename)}")
    return filename


# ============================================================
# Rotation des anciens backups
# ============================================================

def rotate_backups(dest_dir: Path, keep_days: int) -> None:
    """
    Supprime les fichiers de backup plus vieux que keep_days jours.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
    removed = 0
    for f in dest_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix not in (".gz", ".sql"):
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        log(f"Rotation : {removed} fichier(s) supprime(s) (> {keep_days} jours)")
    else:
        log(f"Rotation : rien a supprimer (seuil : {keep_days} jours)")


# ============================================================
# Point d entree
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Sauvegarde QuickSellPay")
    parser.add_argument("--mysql-only",  action="store_true", help="Base de donnees seulement")
    parser.add_argument("--images-only", action="store_true", help="Images seulement")
    parser.add_argument("--keep",        type=int, default=DEFAULT_KEEP_DAYS,
                        help=f"Jours de retention (defaut: {DEFAULT_KEEP_DAYS})")
    parser.add_argument("--dry-run",     action="store_true", help="Simuler sans ecrire")
    args = parser.parse_args()

    do_db     = not args.images_only
    do_images = not args.mysql_only

    log("=" * 55)
    log("QuickSellPay -- Backup")
    log(f"  DB engine  : {DB_ENGINE}")
    log(f"  Backup dir : {BACKUP_DIR}")
    log(f"  Retention  : {args.keep} jours")
    log(f"  Dry run    : {args.dry_run}")
    log("=" * 55)

    # Creer le repertoire de backup
    if not args.dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    errors = 0

    if do_db:
        if DB_ENGINE == "mysql":
            result = backup_mysql(BACKUP_DIR, dry_run=args.dry_run)
            if result is None:
                errors += 1
        else:
            results = backup_sqlite(BACKUP_DIR, dry_run=args.dry_run)
            if not results:
                log("  Aucun fichier SQLite sauvegarde")

    if do_images:
        result = backup_images(BACKUP_DIR, dry_run=args.dry_run)

    if not args.dry_run:
        rotate_backups(BACKUP_DIR, keep_days=args.keep)

    log("=" * 55)
    if errors:
        log(f"Backup termine avec {errors} erreur(s)")
        return 1
    log("Backup termine avec succes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
