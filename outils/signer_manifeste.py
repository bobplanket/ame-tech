"""Signe manifest.json (signature Ed25519 détachée, base64) pour la publication.

La graine privée (32 octets, base64) est lue dans la variable d'environnement
AMETECH_SIGNING_KEY, jamais en argument, jamais affichée. Le script refuse de
signer si la clé publique dérivée de cette graine n'est pas celle du dépôt
(windows/ametech/cle_publique.txt), ou si le dépôt porte encore la clé de test.

Usage (CI) : AMETECH_SIGNING_KEY=... python outils/signer_manifeste.py dist/manifest.json
Sortie     : dist/manifest.json.sig (64 octets en base64, suivi d'un saut de ligne)
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

DEPOT = Path(__file__).resolve().parents[1]
CLE_PUBLIQUE = DEPOT / "windows" / "ametech" / "cle_publique.txt"
MARQUEUR_CLE_TEST = "CLE-DE-TEST"


def charger_ed25519():
    spec = importlib.util.spec_from_file_location("ametech_ed25519", DEPOT / "windows" / "ametech" / "ed25519.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lire_cle_publique(chemin: Path) -> tuple[bytes, bool]:
    texte = chemin.read_text(encoding="utf-8")
    lignes = [l.strip() for l in texte.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lignes:
        raise SystemExit(f"Aucune clé dans {chemin}")
    cle = base64.b64decode(lignes[0], validate=True)
    if len(cle) != 32:
        raise SystemExit(f"Clé publique invalide dans {chemin}")
    return cle, MARQUEUR_CLE_TEST in texte


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifeste", type=Path)
    parser.add_argument("--sortie", type=Path, default=None)
    parser.add_argument("--cle-publique", type=Path, default=CLE_PUBLIQUE)
    parser.add_argument("--autoriser-cle-test", action="store_true", help="tests automatiques seulement")
    args = parser.parse_args(argv)

    brute = os.environ.get("AMETECH_SIGNING_KEY", "").strip()
    if not brute:
        print("AMETECH_SIGNING_KEY absente : rien n'est signé.", file=sys.stderr)
        return 2
    try:
        graine = base64.b64decode(brute, validate=True)
    except ValueError:
        graine = b""
    if len(graine) != 32:
        print("AMETECH_SIGNING_KEY doit contenir 32 octets en base64.", file=sys.stderr)
        return 2

    ed25519 = charger_ed25519()
    attendue, est_test = lire_cle_publique(args.cle_publique)
    if est_test and not args.autoriser_cle_test:
        print("Le dépôt porte encore la clé publique de TEST : lancez outils/generer_cles.py avant de publier.",
              file=sys.stderr)
        return 3
    if ed25519.cle_publique(graine) != attendue:
        print("La graine AMETECH_SIGNING_KEY ne correspond pas à cle_publique.txt : signature refusée.", file=sys.stderr)
        return 3

    brut = args.manifeste.read_bytes()
    manifeste = json.loads(brut.decode("utf-8"))
    if not isinstance(manifeste, dict) or not isinstance(manifeste.get("version"), str) or "zip" not in manifeste:
        print("Manifeste mal formé.", file=sys.stderr)
        return 4
    signature = ed25519.signer(graine, brut)
    if not ed25519.verifier(attendue, brut, signature):
        print("Contrôle de la signature produite en échec.", file=sys.stderr)
        return 5
    sortie = args.sortie or args.manifeste.with_name(args.manifeste.name + ".sig")
    sortie.write_text(base64.b64encode(signature).decode("ascii") + "\n", encoding="ascii")
    print(f"Manifeste {manifeste['version']} signé : {sortie}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
