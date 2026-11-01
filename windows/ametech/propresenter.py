"""Lecture locale de ProPresenter (API v1) et mise en forme des textes.

Repris du connecteur 2.1.0 (connecteur_v2.py et passerelle.py) : détection du
processus et du port, flux /v1/status/updates (JSON CRLF-CRLF ou SSE), repli
HTTP en lecture bornée, et reconnaissance chant / verset. Aucune commande
n'est envoyée à ProPresenter par le relais ; seule la molette technique peut
le faire, et seulement vers 127.0.0.1.
"""
from __future__ import annotations
import codecs
import csv
from dataclasses import dataclass, asdict
import http.client
import io
import json
import os
import re
import socket
import subprocess
import unicodedata
import urllib.error
import urllib.request

MAX_TRAME = 2 * 1024 * 1024
CHEMINS_STATUT = ["status/slide", "status/layers", "presentation/slide_index", "timer/system_time"]
PORT_DEFAUT = 50001


class ErreurReseau(Exception):
    def __init__(self, message, statut=0, raison=""):
        super().__init__(message)
        self.statut = statut
        self.raison = raison


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OUVREUR = urllib.request.build_opener(_SansRedirection(), urllib.request.ProxyHandler({}))


def requete(port: int, chemin: str, corps=None, *, methode: str | None = None, flux: bool = False, delai: float = 6, brut: bool = False):
    """Requête vers http://127.0.0.1:<port><chemin>. brut=True renvoie (statut, octets) sans lever sur les codes HTTP."""
    port = int(port)
    if not 1 <= port <= 65535:
        raise ErreurReseau("Port invalide")
    entetes = {"Accept": "application/json"}
    donnees = None if corps is None else json.dumps(corps, ensure_ascii=False).encode()
    if donnees is not None:
        entetes["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:" + str(port) + chemin, data=donnees, headers=entetes, method=methode)
    try:
        reponse = _OUVREUR.open(req, timeout=delai)
        if flux:
            return reponse
        with reponse:
            contenu = reponse.read(MAX_TRAME + 1)
            statut = reponse.status
        if len(contenu) > MAX_TRAME:
            raise ErreurReseau("Réponse trop volumineuse")
        if brut:
            return statut, contenu
        return json.loads(contenu) if contenu.strip() else None
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            contenu = exc.read(MAX_TRAME)
        except OSError:
            contenu = b""
        exc.close()
        if brut:
            return code, contenu
        raise ErreurReseau("Réponse HTTP " + str(code), code) from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        cause = getattr(exc, "reason", exc)
        numero = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
        if numero in {13, 10013}:
            message = "Accès réseau refusé par Windows"
        elif numero in {61, 111, 10061}:
            message = "Connexion refusée sur ce port"
        elif isinstance(cause, (TimeoutError, socket.timeout)):
            message = "Délai de réponse dépassé"
        elif isinstance(exc, ValueError):
            message = "Réponse JSON invalide"
        else:
            message = "Connexion interrompue ou réponse HTTP invalide"
        raise ErreurReseau(message + (" (code " + str(numero) + ")" if numero else "")) from None


def objets_flux(morceaux):
    """Accepte JSON CRLF-CRLF et SSE, même coupés au milieu d'un caractère UTF-8."""
    decodeur, tampon = codecs.getincrementaldecoder("utf-8")(), ""
    for morceau in morceaux:
        tampon += decodeur.decode(morceau)
        if len(tampon) > MAX_TRAME:
            raise ErreurReseau("Flux trop volumineux")
        tampon = tampon.replace("\r\n", "\n")
        while "\n\n" in tampon:
            trame, tampon = tampon.split("\n\n", 1)
            lignes = trame.splitlines()
            donnees = "\n".join(l[5:].lstrip() for l in lignes if l.startswith("data:"))
            if not donnees:
                donnees = trame.strip() if trame.lstrip().startswith(("{", "[")) else ""
            if not donnees:
                continue
            try:
                valeur = json.loads(donnees)
            except ValueError:
                raise ErreurReseau("Flux JSON invalide") from None
            if isinstance(valeur, dict):
                yield valeur


def ports_windows() -> list[int]:
    """Ports d'écoute des processus ProPresenter uniquement, sans balayage réseau."""
    if os.name != "nt":
        return []
    drapeaux = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        taches = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=4, creationflags=drapeaux)
        pids = {r[1] for r in csv.reader(io.StringIO(taches.stdout)) if len(r) > 1 and r[0].lower().startswith("propresenter")}
        sockets = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=4, creationflags=drapeaux)
        ports = set()
        for ligne in sockets.stdout.splitlines():
            parts = ligne.split()
            if len(parts) >= 5 and parts[-1] in pids and parts[2].endswith(":0"):
                ports.add(int(parts[1].rsplit(":", 1)[1]))
        return sorted(ports)[:20]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def processus_present() -> bool | None:
    """True/False sous Windows, None ailleurs (inconnu)."""
    if os.name != "nt":
        return None
    try:
        drapeaux = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        taches = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=4, creationflags=drapeaux)
        return any(len(r) > 1 and r[0].lower().startswith("propresenter") for r in csv.reader(io.StringIO(taches.stdout)))
    except (OSError, subprocess.TimeoutExpired):
        return None


def decouvrir(prefere: int = 0, dernier: int = 0) -> tuple[int, str]:
    """(port, description) du ProPresenter local. Port choisi, puis dernier connu, 50001, puis ports du processus."""
    essayes, echecs = set(), []

    def sonder(ports):
        for port in ports:
            if not port or port in essayes:
                continue
            essayes.add(port)
            try:
                r = requete(port, "/version", delai=3 if port in {prefere, dernier} else 1)
                if isinstance(r, dict) and r.get("api_version") == "v1" and "propresenter" in str(r.get("host_description", "")).lower():
                    return int(port), str(r.get("host_description", "ProPresenter"))
                echecs.append("Port " + str(port) + " : API ProPresenter non reconnue")
            except ErreurReseau as exc:
                echecs.append("Port " + str(port) + " : " + str(exc))
        return None
    trouve = sonder([prefere, dernier, PORT_DEFAUT]) or sonder(ports_windows())
    if trouve:
        return trouve
    raise ErreurReseau(echecs[0] if echecs else "ProPresenter introuvable : ouvrir ProPresenter et activer Réseau")


def version_depuis(description: str) -> str:
    m = re.search(r"\d+(?:\.\d+)+", description or "")
    return m.group(0) if m else ""


# ---------------------------------------------------------------- textes

def normaliser(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texte.casefold()) if not unicodedata.combining(c) and c.isalnum())


LIVRES = {normaliser(l) for l in """
genese gen genesis ge gn exode exodus ex exo levitique leviticus le lv
nombres numbers nb nm deutéronome deuteronomy dt deu josue joshua jos
juges judges jug jdg ruth rt 1samuel 2samuel 1sa 2sa 1s 2s
1rois 2rois 1r 2r 1kings 2kings 1chroniques 2chroniques 1ch 2ch
1chronicles 2chronicles esdras ezra esd nehemie nehemiah ne neh
esther est job jb psaume psaumes psalm psalms ps
proverbes proverbs pr pro ecclesiaste ecclesiastes ec ecc
cantique cantiques cantiquedescantiques songofsolomon cant ct
esaie isaie isaiah es isa jeremie jeremiah jer jr
lamentations lamentation la lam ezechiel ezekiel ez eze
daniel dan dn osee hosea os ho joel jl amos am abdias obadiah ab ob
jonas jonah jon michée micah mi nahum na habacuc habakkuk ha hab
sophonie zephaniah so zep aggee haggai ag hag zacharie zechariah za zec
malachie malachi mal ml matthieu matthew mt matt marc mark mc mk
luc luke lc lk jean john jn jhn actes acts ac romains romans ro rm rom
1corinthiens 2corinthiens 1corinthians 2corinthians 1co 2co
galates galatians ga gal ephesiens ephesians ep eph
philippiens philippians ph php phil colossiens colossians col
1thessaloniciens 2thessaloniciens 1thessalonians 2thessalonians 1th 2th
1timothee 2timothee 1timothy 2timothy 1ti 2ti tite titus tt tit
philemon phm hebreux hebrews he heb jacques james ja jc jas
1pierre 2pierre 1peter 2peter 1pi 2pi 1p 2p 1pe 2pe
1jean 2jean 3jean 1john 2john 3john 1jn 2jn 3jn jude jud
apocalypse revelation ap rev
""".split()}
VERSIONS = {normaliser(v) for v in (
    "LSG", "LSG 1910", "Louis Segond", "Louis Segond 1910", "Segond 21", "S21",
    "NEG", "NEG 1979", "NBS", "Nouvelle Bible Segond", "BDS", "Bible du Semeur",
    "Semeur", "TOB", "PDV", "Parole de Vie", "BFC", "Bible en français courant",
    "Darby", "Martin", "Ostervald", "KJV", "NKJV", "NIV", "ESV", "NLT", "NASB",
)}
# Le tiret U+2013 des références de versets est reconnu tel que ProPresenter le fournit.
REF = re.compile(
    r"(?P<livre>[1-3]?\s*[A-Za-zÀ-ÖØ-öø-ÿ. ]+?)\s*"
    r"(?P<chapitre>\d{1,3})(?:\s*[:.]\s*\d{1,3}"
    r"(?:\s*[-\u2013]\s*(?:\d{1,3}\s*[:.]\s*)?\d{1,3})?"
    r"(?:\s*[,;]\s*\d{1,3}(?:\s*[-\u2013]\s*\d{1,3})?)*)?"
)


def reference_ligne(ligne: str) -> tuple[str, str] | None:
    texte, version = ligne.strip(), ""
    parenthese = re.search(r"\s*\(([^()]+)\)\s*$", texte)
    if parenthese and normaliser(parenthese[1]) in VERSIONS:
        texte, version = texte[:parenthese.start()].strip(), parenthese[1].strip()
    trouve = REF.fullmatch(texte)
    if trouve and normaliser(trouve["livre"]) in LIVRES:
        return texte, version
    return None


@dataclass(frozen=True)
class Contenu:
    genre: str = "off"
    texte: str = ""
    reference: str = ""
    version: str = ""
    titre: str = ""

    def dict(self) -> dict:
        return asdict(self)


def contenu_depuis(instantane: dict, mode: str = "auto") -> Contenu:
    """Même règle que passerelle.py : notes « badaboum:* », référence biblique seule = verset, sinon chant."""
    texte = str(instantane.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not instantane.get("active") or not texte:
        return Contenu()
    notes, genre_note = {}, ""
    for ligne in str(instantane.get("notes") or "").splitlines():
        ligne = ligne.strip()
        if ligne.casefold() in {"badaboum:off", "no-obs"}:
            return Contenu()
        if ligne.casefold() in {"badaboum:chant", "badaboum:verset"}:
            genre_note = ligne.split(":", 1)[1].casefold()
        cle, sep, valeur = ligne.partition(":")
        if sep and cle.casefold() in {"badaboum-ref", "badaboum-version", "badaboum-titre"}:
            notes[cle.casefold()] = valeur.strip()
    lignes = texte.splitlines()
    refs = [(i, r) for i, r in ((i, reference_ligne(l)) for i, l in enumerate(lignes)) if r]
    ref_auto = refs[0] if len(refs) == 1 else None
    reference = notes.get("badaboum-ref", "") or (ref_auto[1][0] if ref_auto else "")
    genre = mode if mode in {"chant", "verset"} else (genre_note or ("verset" if reference else "chant"))
    if genre == "chant":
        return Contenu("chant", texte, titre=notes.get("badaboum-titre", ""))
    version = notes.get("badaboum-version", "") or (ref_auto[1][1] if ref_auto else "")
    retires = set()
    if ref_auto and (not notes.get("badaboum-ref") or normaliser(reference) == normaliser(ref_auto[1][0])):
        retires.add(ref_auto[0])
    for i, ligne in enumerate(lignes):
        if normaliser(ligne) in VERSIONS:
            if not version:
                version = ligne.strip()
            retires.add(i)
    corps = "\n".join(l for i, l in enumerate(lignes) if i not in retires).strip()
    if not corps:
        return Contenu()
    return Contenu("verset", corps, reference=reference, version=version)


def lire_instantane(port: int) -> tuple[bool, dict]:
    """Couches encadrant la diapo : si l'une des deux lectures dit « éteint », c'est éteint."""
    avant = requete(port, "/v1/status/layers")
    diapo = requete(port, "/v1/status/slide")
    apres = requete(port, "/v1/status/layers")
    if not all(isinstance(x, dict) for x in (avant, diapo, apres)):
        raise ErreurReseau("Format de réponse inattendu pour les couches ou la diapositive")
    actif = avant.get("slide") is True and apres.get("slide") is True
    courante = diapo.get("current") or {}
    if not isinstance(courante, dict) or not isinstance(courante.get("text") or "", str):
        raise ErreurReseau("Format de texte de diapositive inattendu")
    if not isinstance(courante.get("notes") or "", str):
        courante = {**courante, "notes": ""}
    return actif, courante


def ouvrir_flux(port: int, chemins=None):
    return requete(port, "/v1/status/updates", list(chemins or CHEMINS_STATUT), flux=True, delai=6)


def morceaux(reponse, arret=None):
    while arret is None or not arret.is_set():
        part = reponse.read1(4096)
        if not part:
            raise ErreurReseau("Flux ProPresenter fermé")
        yield part
