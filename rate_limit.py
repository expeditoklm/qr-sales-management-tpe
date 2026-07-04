"""
rate_limit.py — v5.0
Rate limiting via Redis (sliding window) avec fallback in-memory automatique.

Stratégie :
  • Si REDIS_URL est configuré et Redis accessible → sliding window Redis
    (partagé entre tous les workers uvicorn/gunicorn)
  • Sinon → in-memory par process (acceptable pour 1 seul worker)

Redis sliding window :
  Deux compteurs par fenêtre temporelle (courante + précédente).
  Estimation : count_prev * (1 - fraction_écoulée) + count_curr
  Précis à ~0.1% près, zéro lock distribué.
"""
import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import HTTPException, Request

# Import Redis optionnel
try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window       = window_seconds
        self._redis       = None
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock        = Lock()
        self._init_redis()

    # ── Initialisation Redis ──────────────────────────────────────────────────

    def _init_redis(self):
        if not _REDIS_AVAILABLE:
            print("[RateLimit] redis-py non installé → fallback in-memory")
            return
        try:
            from config import get_settings
            cfg = get_settings()
            url = getattr(cfg, "REDIS_URL", "")
            if not url:
                print("[RateLimit] REDIS_URL non configuré → fallback in-memory")
                return
            client = _redis_lib.from_url(
                url,
                decode_responses=True,
                socket_timeout=1,
                socket_connect_timeout=1,
            )
            client.ping()
            self._redis = client
            print(f"[RateLimit] Redis connecté → {url}")
        except Exception as exc:
            print(f"[RateLimit] Redis indisponible ({exc}) → fallback in-memory")
            self._redis = None

    # ── Extraction IP ─────────────────────────────────────────────────────────

    @staticmethod
    def _ip(request: Request) -> str:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    # ── Interface publique ────────────────────────────────────────────────────

    def check(self, request: Request):
        ip = self._ip(request)
        if self._redis:
            self._check_redis(ip)
        else:
            self._check_memory(ip)

    # ── Backend Redis (sliding window approximé) ──────────────────────────────

    def _check_redis(self, ip: str):
        now      = time.time()
        slot     = int(now) // self.window
        curr_key = f"rl:{ip}:{slot}"
        prev_key = f"rl:{ip}:{slot - 1}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(curr_key)
            pipe.expire(curr_key, self.window * 2)
            pipe.get(prev_key)
            results = pipe.execute()
            curr_count = int(results[0])
            prev_count = int(results[2] or 0)
            fraction   = (now % self.window) / self.window
            estimated  = prev_count * (1.0 - fraction) + curr_count
            if estimated > self.max_requests:
                retry = int(self.window - (now % self.window)) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Trop de requêtes. Réessayez dans {retry}s.",
                    headers={"Retry-After": str(retry)},
                )
        except HTTPException:
            raise
        except Exception:
            # Redis a planté en cours de route → bascule silencieuse
            self._check_memory(ip)

    # ── Backend in-memory (sliding window exact) ──────────────────────────────

    def _check_memory(self, ip: str):
        now = time.monotonic()
        with self._lock:
            dq = self._windows[ip]
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max_requests:
                retry = int(self.window - (now - dq[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Trop de requêtes. Réessayez dans {retry}s.",
                    headers={"Retry-After": str(retry)},
                )
            dq.append(now)


def make_limiter(max_requests: int, window_seconds: int) -> RateLimiter:
    return RateLimiter(max_requests, window_seconds)
