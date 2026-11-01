"""Assistance à distance : RustDesk configuré sur le serveur AME Tech.

Au premier appel, `lancer_rustdesk()` télécharge le client RustDesk officiel
(version épinglée, empreinte SHA-256 en dur) dans
%LOCALAPPDATA%\\AME Tech\\rustdesk\\, le lance sous un nom de fichier qui porte
la configuration du serveur, puis lit l'identifiant du poste à dicter.

Configuration par le nom de l'exécutable (mécanisme officiel RustDesk,
`custom_server.rs`) : `rustdesk-host=<hôte>,key=<clé>,.exe`. Le client lit
l'hôte et la clé dans son propre nom au démarrage, sans droits administrateur
ni installation (`--config` exige une installation et des droits admin).
Voir docs/RUSTDESK.md.

Aucun secret ici : la clé hbbs est la clé publique du serveur.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Serveur RustDesk auto-hébergé (VPS TMG-S2, /opt/ame-tech-rustdesk).
SERVEUR = "tech.ame.paris"
# Clé publique hbbs (data/id_ed25519.pub sur le serveur). Publique par nature.
CLE_PUBLIQUE = "El2YXaFcYnktAqPPA8U4s1Ytro0MQRsI0+Arvni0td0="

# Client Windows x64 épinglé : release officielle GitHub rustdesk/rustdesk.
VERSION_RUSTDESK = "1.5.0"
URL_RUSTDESK = (
    "https://github.com/rustdesk/rustdesk/releases/download/"
    "1.5.0/rustdesk-1.5.0-x86_64.exe"
)
SHA256_RUSTDESK = "8555777215510d83d2d61c9dc984e4fcc838bd7e79f9d18a42585431f5e8bb47"
TAILLE_RUSTDESK = 25887600

CONTACT = {
    "nom": "Will Hardy",
    "whatsapp": "+1 (438) 458-2683",
    "wa_me": "https://wa.me/14384582683",
    "courriel": "bonjour@tmgconseil.fr",
}
TEAMVIEWER_URL = "https://www.teamviewer.com/fr/telechargement/windows/"

DELAI_TELECHARGEMENT_S = 30  # délai réseau par opération (connexion, lecture)
DELAI_ID_S = 30  # attente maximale de l'identifiant après le lancement
DELAI_GET_ID_S = 10  # délai d'un appel `--get-id`

_INTERDITS_NOM = set('<>:"/\\|?*,') | {chr(c) for c in range(32)}
_RE_ID = re.compile(r"^\d{6,12}$")
_verrou = threading.Lock()
_EST_WINDOWS = sys.platform == "win32"


class ErreurAssistance(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


def _log(niveau: str, message: str, **details) -> None:
    try:
        from ametech import journal
        journal.log(niveau, "assistance", message, **details)
    except Exception:
        pass


def dossier() -> Path:
    """%LOCALAPPDATA%\\AME Tech\\rustdesk (non créé ici)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "AME Tech" / "rustdesk"


def nom_executable(hote: str = SERVEUR, cle: str = CLE_PUBLIQUE) -> str:
    """Nom de fichier qui configure RustDesk : `rustdesk-host=<hôte>,key=<clé>,.exe`.

    La virgule finale protège la clé si Windows ajoute « (1) » au nom.
    Refuse une valeur qui ne tiendrait pas dans un nom de fichier Windows
    (une clé contenant « / » imposerait la chaîne de configuration chiffrée).
    """
    for etiquette, valeur in (("hôte", hote), ("clé", cle)):
        if not valeur or any(c in _INTERDITS_NOM for c in valeur):
            raise ValueError(f"{etiquette} inutilisable dans un nom de fichier : {valeur!r}")
    return f"rustdesk-host={hote},key={cle},.exe"


def chemin_executable() -> Path:
    return dossier() / nom_executable()


def _fichier_id() -> Path:
    return dossier() / "id.txt"


def _id_memorise() -> str | None:
    try:
        valeur = _fichier_id().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return valeur if _RE_ID.match(valeur) else None


def _memoriser_id(valeur: str) -> None:
    try:
        _fichier_id().write_text(valeur, encoding="utf-8")
    except OSError:
        pass


def sha256_fichier(chemin: Path) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def telecharger(url: str, destination: Path, sha256: str, taille: int | None = None) -> Path:
    """Télécharge `url` vers `destination` si et seulement si l'empreinte colle.

    Écrit d'abord dans un fichier `.part`, vérifie taille et SHA-256, puis
    renomme. En cas d'écart, rien n'est conservé et ErreurAssistance est levée.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partiel = destination.with_name(destination.name + ".part")
    h = hashlib.sha256()
    recu = 0
    try:
        requete = urllib.request.Request(url, headers={"User-Agent": "AME-Tech-assistance"})
        with urllib.request.urlopen(requete, timeout=DELAI_TELECHARGEMENT_S) as rep, \
                open(partiel, "wb") as f:
            for bloc in iter(lambda: rep.read(1 << 16), b""):
                recu += len(bloc)
                if taille is not None and recu > taille:
                    raise ErreurAssistance("Le fichier RustDesk reçu est plus gros que prévu, refusé.")
                h.update(bloc)
                f.write(bloc)
        if taille is not None and recu != taille:
            raise ErreurAssistance(
                f"Le fichier RustDesk reçu est incomplet ({recu} octets sur {taille}), refusé.")
        obtenue = h.hexdigest()
        if obtenue != sha256.lower():
            _log("erreur", "Empreinte RustDesk différente, fichier refusé",
                 attendue=sha256, obtenue=obtenue)
            raise ErreurAssistance(
                "L'empreinte du fichier RustDesk ne correspond pas à la version attendue : "
                "fichier refusé et supprimé.")
        os.replace(partiel, destination)
        return destination
    except ErreurAssistance:
        raise
    except Exception as e:  # réseau, disque
        raise ErreurAssistance(f"Téléchargement de RustDesk impossible : {e}") from e
    finally:
        try:
            partiel.unlink()
        except OSError:
            pass


def _executable_valide(chemin: Path, verifier_empreinte: bool) -> bool:
    try:
        if chemin.stat().st_size != TAILLE_RUSTDESK:
            return False
    except OSError:
        return False
    return not verifier_empreinte or sha256_fichier(chemin) == SHA256_RUSTDESK


def _rustdesk_en_cours() -> bool:
    """Vrai si un processus rustdesk.exe tourne (le paquet portable lance rustdesk.exe)."""
    if not _EST_WINDOWS:
        return False
    try:
        sortie = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq rustdesk.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return False
    return '"rustdesk.exe"' in sortie.lower()


def _lancer_processus(chemin: Path) -> None:
    drapeaux = 0
    if _EST_WINDOWS:
        drapeaux = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(chemin)], cwd=str(chemin.parent), close_fds=True, creationflags=drapeaux,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def extraire_id(sortie: str) -> str | None:
    """Dernière ligne purement numérique de la sortie de `--get-id`.

    Le paquet portable écrit d'abord ses propres lignes (« skip ... »,
    « executing ... ») avant celle du client.
    """
    for ligne in reversed(sortie.splitlines()):
        ligne = ligne.strip()
        if _RE_ID.match(ligne):
            return ligne
    return None


def _lire_id(chemin: Path) -> str | None:
    try:
        sortie = subprocess.run(
            [str(chemin), "--get-id"], cwd=str(chemin.parent),
            capture_output=True, text=True, errors="replace", timeout=DELAI_GET_ID_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return None
    return extraire_id(sortie or "")


def _attendre_id(chemin: Path) -> str | None:
    limite = time.monotonic() + DELAI_ID_S
    while True:
        valeur = _lire_id(chemin)
        if valeur:
            return valeur
        if time.monotonic() >= limite:
            return None
        time.sleep(1.5)


def etat() -> dict:
    """{"rustdesk": "absent|pret|lance", "id": str|None, "serveur": "tech.ame.paris"}.

    Léger (pas de calcul d'empreinte ni d'appel RustDesk) : appelé à chaque /api/etat.
    """
    if not _executable_valide(chemin_executable(), verifier_empreinte=False):
        statut = "absent"
    elif _rustdesk_en_cours():
        statut = "lance"
    else:
        statut = "pret"
    return {"rustdesk": statut, "id": _id_memorise(), "serveur": SERVEUR}


def lancer_rustdesk() -> dict:
    """Télécharge au besoin, vérifie, lance RustDesk sur notre serveur, renvoie l'identifiant.

    Réponse : etat() + {"ok": bool, "message": str}.
    """
    if not _EST_WINDOWS:
        return {**etat(), "ok": False,
                "message": "L'assistance RustDesk n'est disponible que sous Windows."}
    if not _verrou.acquire(blocking=False):
        return {**etat(), "ok": False,
                "message": "RustDesk est déjà en cours de préparation, patientez quelques secondes."}
    try:
        chemin = chemin_executable()
        if not _executable_valide(chemin, verifier_empreinte=True):
            _log("info", "Téléchargement de RustDesk", version=VERSION_RUSTDESK)
            try:
                chemin.unlink()
            except OSError:
                pass
            telecharger(URL_RUSTDESK, chemin, SHA256_RUSTDESK, TAILLE_RUSTDESK)
            _log("info", "RustDesk téléchargé et vérifié", sha256=SHA256_RUSTDESK)
        try:
            _lancer_processus(chemin)
        except OSError as e:
            raise ErreurAssistance(f"Impossible de lancer RustDesk : {e}") from e
        _log("info", "RustDesk lancé", serveur=SERVEUR)
        identifiant = _attendre_id(chemin)
        if identifiant:
            _memoriser_id(identifiant)
            message = (f"RustDesk est ouvert. Dictez à {CONTACT['nom']} l'identifiant "
                       f"{identifiant} et le mot de passe affiché dans la fenêtre RustDesk.")
        else:
            _log("alerte", "Identifiant RustDesk non lu", delai_s=DELAI_ID_S)
            message = ("RustDesk est ouvert, mais l'identifiant n'a pas pu être lu "
                       f"automatiquement en {DELAI_ID_S} secondes. Lisez l'identifiant et le "
                       f"mot de passe dans la fenêtre RustDesk et dictez-les à {CONTACT['nom']}.")
        resultat = etat()
        if identifiant:
            resultat["id"] = identifiant
        return {**resultat, "ok": True, "message": message}
    except ErreurAssistance as e:
        _log("erreur", str(e))
        return {**etat(), "ok": False, "message": str(e)}
    finally:
        _verrou.release()
