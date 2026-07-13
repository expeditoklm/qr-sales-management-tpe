"""
Installe les dépendances Python de QuickSellPay.

Usage :
    python install_requirements.py
"""
from pathlib import Path
import subprocess
import sys


BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = BASE_DIR / "requirements.txt"


def main() -> int:
    if not REQUIREMENTS.exists():
        print(f"ERREUR : fichier introuvable : {REQUIREMENTS}")
        return 1

    command = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    print(f"Python utilisé : {sys.executable}")
    print(f"Installation depuis : {REQUIREMENTS}")

    result = subprocess.run(command, cwd=BASE_DIR)
    if result.returncode == 0:
        print("\nOK : dépendances installées avec succès.")
        return 0

    print(f"\nERREUR : pip s'est arrêté avec le code {result.returncode}.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
