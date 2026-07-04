"""
gunicorn.conf.py -- Configuration Gunicorn pour QuickSellPay
============================================================
Serveur WSGI/ASGI multi-workers optimise pour cPanel (Tunelaf).

Lancement en production :
    gunicorn -c gunicorn.conf.py main:app

Lancement dev (1 worker, rechargement auto) :
    gunicorn -c gunicorn.conf.py --workers 1 --reload main:app

Notes cPanel :
  - cPanel Passenger gere le demarrage/arret automatiquement
  - Ce fichier sert pour lancer Gunicorn directement (SSH ou cron restart)
  - Le fichier passenger_wsgi.py reste le point d entree cPanel
"""

import multiprocessing
import os
from pathlib import Path

# -- Chemins
BASE_DIR  = Path(__file__).parent
LOG_DIR   = BASE_DIR / "logs"
RUN_DIR   = BASE_DIR / "tmp"
LOG_DIR.mkdir(exist_ok=True)
RUN_DIR.mkdir(exist_ok=True)

# ============================================================
# Workers
# ============================================================
# Formule recommandee : (2 x CPU) + 1
# Sur cPanel shared hosting, limiter a 2-4 pour ne pas saturer
# la memoire partagee avec d autres clients.
_cpu_count = multiprocessing.cpu_count()
_env_workers = os.environ.get("GUNICORN_WORKERS")

if _env_workers:
    workers = int(_env_workers)
else:
    # 2 workers minimum, 4 maximum sur shared hosting
    workers = min(max((_cpu_count * 2) + 1, 2), 4)

# Classe worker : UvicornWorker pour FastAPI (ASGI)
worker_class = "uvicorn.workers.UvicornWorker"

# Threads par worker (UvicornWorker ignore cette valeur, mais utile si on passe en sync)
threads = 2

# ============================================================
# Reseau
# ============================================================
# Ecouter sur un socket Unix (recommande cPanel) ou TCP
_socket_path = str(RUN_DIR / "gunicorn.sock")
_bind_tcp    = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# Utiliser socket Unix si le repertoire tmp/ est accessible
if RUN_DIR.exists() and os.access(str(RUN_DIR), os.W_OK):
    bind = f"unix:{_socket_path}"
else:
    bind = _bind_tcp

# ============================================================
# Timeouts
# ============================================================
# Timeout general : tue un worker qui ne repond pas en N secondes
# Mettre a 120s pour les exports PDF ou ZIP volumineux
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

# Keepalive : maintenir les connexions HTTP persistantes
keepalive = 5

# Graceful shutdown : attendre N secondes avant de forcer l arret
graceful_timeout = 30

# ============================================================
# Restart automatique des workers (anti-memory-leak)
# ============================================================
# Recycler un worker apres N requetes (0 = desactive)
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))

# Variation aleatoire pour eviter que tous les workers redemarrent en meme temps
max_requests_jitter = 100

# ============================================================
# Logs
# ============================================================
loglevel      = os.environ.get("GUNICORN_LOG_LEVEL", "info")
accesslog     = str(LOG_DIR / "gunicorn_access.log")
errorlog      = str(LOG_DIR / "gunicorn_error.log")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sus'

# ============================================================
# PID file
# ============================================================
pidfile = str(RUN_DIR / "gunicorn.pid")

# ============================================================
# Process name (visible dans ps aux)
# ============================================================
proc_name = "quicksellpay"

# ============================================================
# Hooks (events de cycle de vie)
# ============================================================

def on_starting(server):
    server.log.info(
        "QuickSellPay demarrage -- %d worker(s) %s",
        workers, worker_class
    )

def post_fork(server, worker):
    """Appele apres la creation de chaque worker."""
    server.log.info("Worker %s demarre (PID %s)", worker.age, worker.pid)

def worker_exit(server, worker):
    """Appele quand un worker se termine."""
    server.log.warning("Worker %s (PID %s) arrete", worker.age, worker.pid)

def on_exit(server):
    server.log.info("QuickSellPay arret propre")

# ============================================================
# Resume de la configuration (affiche au demarrage)
# ============================================================

if __name__ == "__main__":
    # Mode diagnostic : affiche la config resolue sans lancer le serveur
    print(f"workers         : {workers}")
    print(f"worker_class    : {worker_class}")
    print(f"bind            : {bind}")
    print(f"timeout         : {timeout}s")
    print(f"max_requests    : {max_requests} (+/- {max_requests_jitter})")
    print(f"loglevel        : {loglevel}")
    print(f"accesslog       : {accesslog}")
    print(f"errorlog        : {errorlog}")
