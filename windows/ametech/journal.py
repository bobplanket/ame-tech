"""Journal du logiciel : anneau mémoire et fichier tournant, sans aucun secret.

Chaque ligne passe par `masquer()` : secret TOTP, codes à 6 chiffres, cookies,
en-têtes d'authentification et chaînes d'enrôlement sont remplacés avant
d'entrer dans la mémoire, le fichier ou le rapport.
"""
from __future__ import annotations
import collections
import io
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
import zipfile

TAILLE_ANNEAU = 2000
TAILLE_FICHIER = 1024 * 1024
NB_FICHIERS = 3
NIVEAUX = ("info", "alerte", "erreur")

_verrou = threading.RLock()
_anneau: collections.deque = collections.deque(maxlen=TAILLE_ANNEAU)
_compteur = 0
_sensibles: set[str] = set()
_dossier: Path | None = None
# Fournit le diagnostic du relais pour le rapport (posé par ui_server).
fournisseur_diagnostic = None

_MOTIFS = [
    (re.compile(r"AMETECH1:[A-Za-z2-7=]+", re.I), "AMETECH1:***"),
    (re.compile(r"otpauth://\S+", re.I), "otpauth://***"),
    (re.compile(r"(secret[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9+/=_-]{8,}", re.I), r"\1***"),
    (re.compile(r"(badaboum_session|ame_admin)=[^;\s\"']+", re.I), r"\1=***"),
    (re.compile(r"((?:cookie|set-cookie|x-code|x-pin|x-jeton|authorization)[\"']?\s*[:=]\s*[\"']?)[^\"'\n,}]+", re.I), r"\1***"),
    (re.compile(r"(code[\"']?\s*[:=]\s*[\"']?)\d{6}\b", re.I), r"\1******"),
    # Six chiffres isolés : le plus souvent un code du moment. Une heure, un port ou une date gardent leurs séparateurs.
    (re.compile(r"(?<![\d.:/-])\d{6}(?![\d.:/-])"), "******"),
]


def dossier() -> Path:
    """Même dossier que les réglages : %APPDATA%\\AME Tech, ou AMETECH_DOSSIER pour les tests."""
    if _dossier is not None:
        return _dossier
    from ametech import coffre
    return coffre.dossier()


def definir_dossier(chemin) -> None:
    global _dossier
    _dossier = Path(chemin) if chemin else None


def enregistrer_sensible(valeur: str) -> None:
    """Toute occurrence de cette valeur sera masquée (secret TOTP, cookie de session)."""
    valeur = str(valeur or "")
    if len(valeur) >= 6:
        with _verrou:
            _sensibles.add(valeur)
            _sensibles.add(valeur.upper())
            _sensibles.add(valeur.lower())


def oublier_sensibles() -> None:
    with _verrou:
        _sensibles.clear()


def masquer(texte) -> str:
    texte = str(texte)
    with _verrou:
        sensibles = sorted(_sensibles, key=len, reverse=True)
    for valeur in sensibles:
        if valeur in texte:
            texte = texte.replace(valeur, "***")
    for motif, remplacement in _MOTIFS:
        texte = motif.sub(remplacement, texte)
    return texte


def _masquer_objet(valeur):
    if isinstance(valeur, dict):
        return {str(k): ("***" if str(k).lower() in {"secret", "secret_totp", "code", "cookie", "jeton", "x-code", "password", "texte_enrolement"} else _masquer_objet(v))
                for k, v in valeur.items()}
    if isinstance(valeur, (list, tuple)):
        return [_masquer_objet(v) for v in valeur]
    if isinstance(valeur, (int, float, bool)) or valeur is None:
        return valeur
    return masquer(valeur)


def _fichier() -> Path | None:
    try:
        d = dossier()
        d.mkdir(parents=True, exist_ok=True)
        return d / "journal.log"
    except OSError:
        return None


def _ecrire_fichier(ligne: dict) -> None:
    chemin = _fichier()
    if chemin is None:
        return
    try:
        if chemin.exists() and chemin.stat().st_size > TAILLE_FICHIER:
            for i in range(NB_FICHIERS - 1, 0, -1):
                ancien = chemin.with_name(f"journal.log.{i}")
                if i == NB_FICHIERS - 1 and ancien.exists():
                    ancien.unlink()
                precedent = chemin if i == 1 else chemin.with_name(f"journal.log.{i - 1}")
                if precedent.exists():
                    os.replace(precedent, ancien)
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log(niveau: str, etape: str, message: str, **details) -> None:
    global _compteur
    if niveau not in NIVEAUX:
        niveau = "info"
    ligne = {"t": round(time.time(), 3), "niveau": niveau, "etape": masquer(etape)[:80], "message": masquer(message)[:2000]}
    if details:
        ligne["details"] = _masquer_objet(details)
    with _verrou:
        _compteur += 1
        ligne = {"n": _compteur, **ligne}
        _anneau.append(ligne)
    _ecrire_fichier(ligne)


def dernieres(n: int = 500) -> list[dict]:
    with _verrou:
        lignes = list(_anneau)
    return [dict(l) for l in lignes[-max(0, int(n)):]] if n else []


def depuis(n: int) -> tuple[list[dict], int]:
    """Lignes de numéro strictement supérieur à n, et le dernier numéro connu."""
    with _verrou:
        lignes = [dict(l) for l in _anneau if l["n"] > n]
        return lignes, _compteur


def rapport_zip(diagnostic: dict | None = None) -> bytes:
    """ZIP de dépannage : journal, diagnostic, réglages sans secret. Tout passe par masquer()."""
    if diagnostic is None and callable(fournisseur_diagnostic):
        try:
            diagnostic = fournisseur_diagnostic()
        except Exception as exc:  # le rapport doit sortir même si le diagnostic échoue
            diagnostic = {"erreur": type(exc).__name__}
    from ametech.version import VERSION
    reglages = {}
    try:
        from ametech import coffre
        reglages = {k: v for k, v in coffre.lire().items() if k != "secret_totp"}
        reglages["enrole"] = bool(coffre.lire().get("secret_totp"))
    except Exception:
        pass
    systeme = {"version": VERSION, "python": sys.version.split()[0], "plateforme": platform.platform(),
               "machine": platform.node(), "heure": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("systeme.json", json.dumps(_masquer_objet(systeme), ensure_ascii=False, indent=2))
        z.writestr("reglages.json", json.dumps(_masquer_objet(reglages), ensure_ascii=False, indent=2))
        z.writestr("diagnostic.json", json.dumps(_masquer_objet(diagnostic or {}), ensure_ascii=False, indent=2))
        z.writestr("journal-memoire.jsonl", "\n".join(json.dumps(l, ensure_ascii=False) for l in dernieres(TAILLE_ANNEAU)))
        chemin = _fichier()
        if chemin is not None:
            for nom in ["journal.log"] + [f"journal.log.{i}" for i in range(1, NB_FICHIERS)]:
                f = chemin.with_name(nom)
                try:
                    if f.exists():
                        z.writestr(nom, masquer(f.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass
    return tampon.getvalue()
