"""Génère la paire de clés Ed25519 de signature des mises à jour AME Tech.

- Écrit la clé publique dans windows/ametech/cle_publique.txt (à commiter).
- Écrit la graine privée (32 octets, base64) UNE seule fois sur la sortie
  standard, et rien d'autre : elle n'est jamais écrite sur disque.

Usage conseillé (la graine ne s'affiche même pas) :
    python outils/generer_cles.py | gh secret set AMETECH_SIGNING_KEY --env release --repo bobplanket/ame-tech

Refuse de remplacer une vraie clé existante sans --remplacer (toutes les
installations en place cesseraient d'accepter les mises à jour).
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import secrets
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cle-publique", type=Path, default=CLE_PUBLIQUE)
    parser.add_argument("--remplacer", action="store_true", help="remplacer une vraie clé déjà en place")
    args = parser.parse_args(argv)

    if args.cle_publique.is_file():
        actuel = args.cle_publique.read_text(encoding="utf-8")
        if MARQUEUR_CLE_TEST not in actuel and not args.remplacer:
            print("Une vraie clé publique est déjà en place. Les postes installés n'accepteraient plus les "
                  "mises à jour signées avec une nouvelle clé. Ajoutez --remplacer si c'est voulu.", file=sys.stderr)
            return 3

    ed25519 = charger_ed25519()
    graine = secrets.token_bytes(32)
    publique = ed25519.cle_publique(graine)
    signature = ed25519.signer(graine, b"controle AME Tech")
    if not ed25519.verifier(publique, b"controle AME Tech", signature):
        print("Contrôle de la paire générée en échec.", file=sys.stderr)
        return 4
    args.cle_publique.write_text(
        "# Clé publique Ed25519 de signature des mises à jour AME Tech (base64, 32 octets).\n"
        "# La graine privée correspondante vit seulement dans le secret GitHub AMETECH_SIGNING_KEY\n"
        "# (environnement « release »). Ne jamais la commiter.\n"
        + base64.b64encode(publique).decode("ascii") + "\n",
        encoding="utf-8",
    )
    print(f"Clé publique écrite dans {args.cle_publique}. Graine privée envoyée sur la sortie standard.",
          file=sys.stderr)
    sys.stdout.write(base64.b64encode(graine).decode("ascii") + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
