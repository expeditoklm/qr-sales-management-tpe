"""
rate_limit.py — Rate limiting en mémoire (sliding window)
En production avec plusieurs workers, utiliser Redis.
"""
import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def _key(self, request: Request) -> str:
        # Utilise l'IP réelle (compatible Nginx proxy_pass + X-Forwarded-For)
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request):
        key = self._key(request)
        now = time.monotonic()
        with self._lock:
            dq = self._windows[key]
            # Purger les entrées hors fenêtre
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max_requests:
                retry_after = int(self.window - (now - dq[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Trop de requêtes. Réessayez dans {retry_after}s.",
                    headers={"Retry-After": str(retry_after)},
                )
            dq.append(now)


def make_limiter(max_requests: int, window_seconds: int) -> RateLimiter:
    return RateLimiter(max_requests, window_seconds)