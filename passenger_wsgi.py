import os
import sys
import traceback

BASE_DIR = os.path.dirname(__file__)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.environ.setdefault("BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
os.environ.setdefault("PUBLIC_APP_BASE_URL", "https://quicksellpay.tunelaf.com")

def _write_passenger_error(message: str) -> None:
    tmp_dir = os.path.join(BASE_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    log_file = os.path.join(tmp_dir, "passenger_error.log")
    with open(log_file, "w", encoding="utf-8") as stream:
        stream.write(message)

def _fallback_app(error_message: str):
    body = (
        "QuickSellPay failed to start.\n\n"
        "Check this file on the server:\n"
        f"{os.path.join(BASE_DIR, 'tmp', 'passenger_error.log')}\n\n"
        "Technical error:\n"
        f"{error_message}"
    ).encode("utf-8")

    def application(environ, start_response):
        start_response(
            "500 Internal Server Error",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    return application

try:
    from a2wsgi import ASGIMiddleware
    from main import app

    application = ASGIMiddleware(app)
except Exception as exc:
    details = traceback.format_exc()
    _write_passenger_error(details)
    application = _fallback_app(str(exc))
