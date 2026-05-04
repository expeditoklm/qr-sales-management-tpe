import traceback
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = TMP_DIR / "diagnostic.log"


def log(message: str) -> None:
    print(message)
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def main() -> int:
    LOG_FILE.write_text("", encoding="utf-8")
    log(f"BASE_DIR={BASE_DIR}")

    try:
        import config  # noqa: F401
        log("config import OK")
    except Exception:
        log("config import FAILED")
        log(traceback.format_exc())
        return 1

    try:
        import database  # noqa: F401
        log("database import OK")
    except Exception:
        log("database import FAILED")
        log(traceback.format_exc())
        return 1

    try:
        import main  # noqa: F401
        log("main import OK")
    except Exception:
        log("main import FAILED")
        log(traceback.format_exc())
        return 1

    try:
        import passenger_wsgi  # noqa: F401
        log("passenger_wsgi import OK")
    except Exception:
        log("passenger_wsgi import FAILED")
        log(traceback.format_exc())
        return 1

    log("Diagnostic termine avec succes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
