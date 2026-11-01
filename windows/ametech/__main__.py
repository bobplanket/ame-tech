"""Point d'entrée : `python -m ametech` (lancé par AME Tech.exe avec pythonw.exe).

Un second lancement rouvre la fenêtre de l'instance active puis s'arrête.
"""
from __future__ import annotations
import argparse
import os
import sys
import threading

from ametech import coffre, journal, ui_server
from ametech.relais import Relais
from ametech.version import VERSION


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ametech", description="AME Tech : relais ProPresenter et code d'accès")
    parser.add_argument("--sans-fenetre", action="store_true", help="ne pas ouvrir la fenêtre")
    parser.add_argument("--port", type=int, default=None, help="port local imposé (sinon 48690 à 48699)")
    args = parser.parse_args(argv)

    existante = ui_server.instance_active()
    if existante:
        if not args.sans_fenetre:
            ui_server.ouvrir_fenetre(existante)
        return 0

    journal.log("info", "demarrage", "AME Tech " + VERSION + " (Python " + sys.version.split()[0] + ")")
    relais = Relais()
    ui = ui_server.ServeurUI(relais)
    try:
        ui.demarrer(args.port)
    except OSError as exc:
        journal.log("erreur", "demarrage", str(exc))
        return 1

    def arret_propre(code: int) -> None:
        journal.log("info", "demarrage", "Arrêt pour relance (code " + str(code) + ")")
        try:
            relais.arreter()
            ui.arreter()
        finally:
            os._exit(code)

    maj = ui_server.maj
    if maj is not None:
        try:
            if hasattr(maj, "definir_arret"):
                maj.definir_arret(arret_propre)
            if hasattr(maj, "boucle"):
                threading.Thread(target=maj.boucle, args=(relais,), name="ametech-maj", daemon=True).start()
        except Exception as exc:
            journal.log("alerte", "maj", "Mises à jour automatiques indisponibles : " + type(exc).__name__)

    reglages = coffre.lire()
    if reglages.get("relais_au_lancement") and reglages.get("secret_totp"):
        relais.demarrer()
    if not args.sans_fenetre:
        ui_server.ouvrir_fenetre(ui.url)
    try:
        ui.servir()
    except KeyboardInterrupt:
        pass
    finally:
        relais.arreter()
    return 0


if __name__ == "__main__":
    sys.exit(main())
