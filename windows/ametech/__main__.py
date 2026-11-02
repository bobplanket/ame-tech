"""Point d'entrée : `python -m ametech` (lancé par AME Tech.exe avec pythonw.exe).

Un second lancement rouvre la fenêtre de l'instance active puis s'arrête.

Code de sortie : 75 demande au lanceur de relancer (mise à jour installée).
Un arrêt pour relance note son code avant tout, et `main` finit par
`os._exit(code)` : le fil principal ne peut plus sortir en 0 pendant que le fil
de la mise à jour arrête l'application. Si le relais tournait, la marque
`relais_apres_maj` du coffre le fait repartir au lancement suivant.
"""
from __future__ import annotations
import argparse
import os
import sys
import threading

from ametech import coffre, journal, ui_server
from ametech.relais import Relais
from ametech.version import VERSION

CODE_RELANCE = 75


def _relance_en_attente() -> bool:
    """Mise à jour installée dont la relance n'a pas encore eu lieu (sortir en 0 la ferait prendre pour un échec)."""
    try:
        return ui_server.maj is not None and ui_server.maj.etat().get("statut") == "pret"
    except Exception:
        return False


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

    verrou_sortie = threading.Lock()
    sortie: dict = {}

    def arret_propre(code: int) -> None:
        with verrou_sortie:
            sortie.setdefault("code", code)  # lu par main avant de sortir : le code demandé gagne
        journal.log("info", "demarrage", "Arrêt pour relance (code " + str(code) + ")")
        try:
            if code == CODE_RELANCE and relais.actif:
                try:
                    coffre.ecrire({"relais_apres_maj": True})
                except Exception as exc:
                    journal.log("alerte", "demarrage", "Reprise du relais après la relance non mémorisée : " + type(exc).__name__)
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
    apres_maj = bool(reglages.get("relais_apres_maj"))
    if reglages.get("secret_totp") and (reglages.get("relais_au_lancement") or apres_maj):
        if apres_maj:
            journal.log("info", "demarrage", "Le relais tournait avant la mise à jour : reprise automatique")
        relais.demarrer()
    if apres_maj:
        try:
            coffre.ecrire({"relais_apres_maj": False})  # après le démarrage : une relance ratée le refera
        except Exception as exc:
            journal.log("alerte", "demarrage", "Marque de reprise du relais non effacée : " + type(exc).__name__)
    if not args.sans_fenetre:
        ui_server.ouvrir_fenetre(ui.url)
    try:
        ui.servir()
    except KeyboardInterrupt:
        pass
    finally:
        relais.arreter()
    with verrou_sortie:
        code = sortie.get("code")
        if code is None and _relance_en_attente():
            code = sortie["code"] = CODE_RELANCE
    if code is not None:
        os._exit(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
