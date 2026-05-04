import os
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def run_step(label: str, command: list[str], allow_failure: bool = False) -> bool:
    print(f"\n=== {label} ===")
    print("Commande:", " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        text=True,
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode != 0:
        print(f"[ERREUR] code={result.returncode}")
        if not allow_failure:
          return False
    else:
        print("[OK]")
    return True


def ensure_directories() -> None:
    print("\n=== Creation des dossiers ===")
    required_dirs = [
        BASE_DIR / "data",
        BASE_DIR / "backups",
        BASE_DIR / "static" / "images",
        BASE_DIR / "tmp",
    ]
    for folder in required_dirs:
        folder.mkdir(parents=True, exist_ok=True)
        print(f"[OK] {folder}")


def show_summary() -> None:
    print("\n=== Resume ===")
    print(f"Python utilise : {sys.executable}")
    print(f"Version Python : {sys.version}")
    print(f"Racine projet   : {BASE_DIR}")
    print("Startup file    : passenger_wsgi.py")
    print("Entry point     : application")
    print("\nSi tout est OK, retournez dans cPanel puis cliquez sur Save / Restart.")


def main() -> int:
    print("Lancement du setup cPanel QuickSellPay...")
    print(f"Dossier de travail: {BASE_DIR}")

    ensure_directories()

    steps = [
        (
            "Mise a jour de pip",
            [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
            True,
        ),
        (
            "Installation des dependances du projet",
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            False,
        ),
        (
            "Installation de a2wsgi pour Passenger",
            [sys.executable, "-m", "pip", "install", "a2wsgi"],
            False,
        ),
    ]

    for label, command, allow_failure in steps:
        if not run_step(label, command, allow_failure=allow_failure):
            print("\nLe setup s'est arrete sur une erreur.")
            print("Corrigez l'erreur affichee puis relancez ce script.")
            return 1

    show_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
