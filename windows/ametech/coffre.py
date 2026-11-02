"""Réglages persistants de AME Tech, secret TOTP chiffré par DPAPI sous Windows.

Fichier : %APPDATA%\\AME Tech\\reglages.json (AMETECH_DOSSIER remplace le dossier,
utile pour les tests). Hors Windows, pas de DPAPI : le secret reste dans le
fichier, protégé par les droits 600.
"""
from __future__ import annotations
import base64
import copy
import ctypes
import json
import os
from pathlib import Path
import secrets
import sys
import threading

DEFAUTS = {
    "secret_totp": "",
    "cibles": ["https://tech.ame.paris"],
    "port_pp": 0,
    "mode": "auto",
    "demarrage_windows": False,
    "relais_au_lancement": False,
    "relais_apres_maj": False,  # le relais tournait avant une relance de mise à jour : le redémarrer
    "canal_maj": "stable",
}
_verrou = threading.RLock()


def dossier() -> Path:
    force = os.environ.get("AMETECH_DOSSIER")
    if force:
        return Path(force)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "AME Tech"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AME Tech"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ame-tech"


def chemin() -> Path:
    return dossier() / "reglages.json"


def protect(valeur: str, dechiffrer: bool = False) -> str:
    """DPAPI (CryptProtectData) : le secret n'est lisible que par ce compte Windows. Vide hors Windows ou en échec."""
    if os.name != "nt":
        return ""

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    try:
        brut = base64.b64decode(valeur, validate=True) if dechiffrer else valeur.encode()
    except (ValueError, TypeError):
        return ""
    garde = ctypes.create_string_buffer(brut)
    source = Blob(len(brut), ctypes.cast(garde, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    fonction = ctypes.windll.crypt32.CryptUnprotectData if dechiffrer else ctypes.windll.crypt32.CryptProtectData
    if not fonction(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        return ""
    try:
        sortie = ctypes.string_at(dest.pbData, dest.cbData)
        return sortie.decode() if dechiffrer else base64.b64encode(sortie).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(dest.pbData)


def _valider(d: dict) -> dict:
    sortie = copy.deepcopy(DEFAUTS)
    for cle, defaut in DEFAUTS.items():
        valeur = d.get(cle, defaut)
        if isinstance(defaut, bool):
            sortie[cle] = valeur if isinstance(valeur, bool) else defaut
        elif isinstance(defaut, int):
            sortie[cle] = valeur if type(valeur) is int and 0 <= valeur <= 65535 else defaut
        elif isinstance(defaut, list):
            sortie[cle] = [str(u) for u in valeur if isinstance(u, str) and u.strip()][:5] if isinstance(valeur, list) else list(defaut)
            if not sortie[cle]:
                sortie[cle] = list(defaut)
        else:
            sortie[cle] = valeur if isinstance(valeur, str) else defaut
    if sortie["mode"] not in ("auto", "manuel"):
        sortie["mode"] = "auto"
    cid = d.get("client_id")
    sortie["client_id"] = cid if isinstance(cid, str) and 16 <= len(cid) <= 64 else ""
    return sortie


def _lire_brut() -> dict:
    try:
        valeur = json.loads(chemin().read_text(encoding="utf-8"))
        return valeur if isinstance(valeur, dict) else {}
    except (OSError, ValueError):
        return {}


def lire() -> dict:
    """Réglages complets, secret déchiffré. Ajoute un client_id stable s'il manque."""
    with _verrou:
        brut = _lire_brut()
        if os.name == "nt":
            chiffre = brut.get("secret_totp_dpapi", "")
            brut["secret_totp"] = protect(chiffre, True) if isinstance(chiffre, str) and chiffre else ""
        d = _valider(brut)
        if not d["client_id"]:
            d["client_id"] = secrets.token_hex(16)
            ecrire(d)
        return d


def ecrire(d: dict) -> None:
    """Écrit tous les réglages (fusionnés avec l'existant), de façon atomique, en 600."""
    with _verrou:
        actuel = _valider(_lire_brut()) if os.name != "nt" else _valider({**_lire_brut(), "secret_totp": ""})
        if os.name == "nt" and "secret_totp" not in d:
            # Sans nouveau secret, on garde le blob DPAPI existant tel quel.
            ancien_blob = _lire_brut().get("secret_totp_dpapi", "")
        else:
            ancien_blob = None
        fusion = _valider({**actuel, **d})
        a_ecrire = {k: v for k, v in fusion.items() if k != "secret_totp"}
        if os.name == "nt":
            if ancien_blob is not None:
                a_ecrire["secret_totp_dpapi"] = ancien_blob
            else:
                a_ecrire["secret_totp_dpapi"] = protect(fusion["secret_totp"]) if fusion["secret_totp"] else ""
                if fusion["secret_totp"] and not a_ecrire["secret_totp_dpapi"]:
                    raise OSError("Chiffrement Windows (DPAPI) indisponible")
        else:
            a_ecrire["secret_totp"] = fusion["secret_totp"]
        d_path = dossier()
        d_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d_path, 0o700)
        except OSError:
            pass
        tmp = chemin().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(a_ecrire, f, ensure_ascii=False, indent=2)
        os.replace(tmp, chemin())
        try:
            os.chmod(chemin(), 0o600)
        except OSError:
            pass


def maj(**champs) -> dict:
    """Modifie quelques champs et renvoie les réglages complets."""
    with _verrou:
        ecrire(champs)
        return lire()
