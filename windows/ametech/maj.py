"""Mise à jour automatique signée d'AME Tech (Windows).

Chaîne de confiance :
1. `manifest.json` et `manifest.json.sig` sont lus sur la première source qui
   répond avec une signature valide (GitHub `releases/latest/download/`, puis
   `https://tech.ame.paris/maj/`).
2. La signature Ed25519 détachée (64 octets en base64) porte sur les octets
   bruts du manifeste. Clé publique : `cle_publique.txt` (une clé marquée
   CLE-DE-TEST désactive toute mise à jour).
3. Version strictement supérieure à la version locale, jamais une version qui a
   déjà échoué au démarrage sur ce poste.
4. Le ZIP est téléchargé (taille plafonnée, délais réseau), son SHA-256 doit
   être celui du manifeste signé, puis chaque fichier extrait doit avoir
   l'empreinte annoncée dans `fichiers` ; un fichier du ZIP absent du manifeste
   fait tout refuser. Rien n'est exécuté avant ces contrôles.
5. Extraction dans `%LOCALAPPDATA%\\AME Tech\\versions\\<version>\\`, bascule du
   pointeur `courante.txt` (l'ancien est noté dans `precedente.txt`), lancement
   d'une garde avec l'ANCIEN moteur, puis fin du processus avec le code 75 :
   le lanceur `AME Tech.exe` relit `courante.txt` et démarre la nouvelle
   version. Si elle ne répond pas sur `http://127.0.0.1:<port>/api/etat` avec
   sa version dans les 30 s, la garde remet l'ancien pointeur, arrête le
   processus défaillant et note l'échec.

Une mise à jour n'est jamais appliquée pendant que le relais tourne (toute phase
autre que `arret`), sauf s'il n'a transmis aucun texte depuis 10 minutes. Le
relais qui tournait est relancé après la mise à jour (`__main__`).

Entrées du manifeste `fichiers` : une entrée dont le `chemin` est un membre du
ZIP décrit ce membre (contrôlé après extraction) ; les autres entrées (par
exemple `ame-tech.lua`) sont des fichiers publiés à part, ignorés ici.

Tout est injectable (racine, sources, clé, version, relance) pour les tests.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import ed25519

SOURCES = (
    "https://github.com/bobplanket/ame-tech/releases/latest/download/",
    "https://tech.ame.paris/maj/",
)
NOM_MANIFESTE = "manifest.json"
NOM_SIGNATURE = "manifest.json.sig"
NOM_ZIP = "AME-Tech-Windows.zip"

TAILLE_MAX_MANIFESTE = 512 * 1024
TAILLE_MAX_SIGNATURE = 1024
TAILLE_MAX_ZIP = 200 * 1024 * 1024
TAILLE_MAX_EXTRAITE = 600 * 1024 * 1024
FICHIERS_MAX = 5000

DELAI_RESEAU = 20.0          # secondes par opération réseau
DELAI_TOTAL_PETIT = 60.0     # manifeste et signature
DELAI_TOTAL_ZIP = 20 * 60.0  # téléchargement du ZIP
INTERVALLE = 6 * 3600.0
REESSAI_OCCUPE = 300.0
INACTIVITE_AVANT_MAJ = 600.0
DELAI_SANTE = 30.0
PORTS_UI = tuple(range(48690, 48700))
CODE_RELANCE = 75
MARQUEUR_CLE_TEST = "CLE-DE-TEST"

FICHIERS_OBLIGATOIRES = (
    "ametech/__main__.py",
    "ametech/version.py",
    "runtime/python/pythonw.exe",
)

_RE_VERSION = re.compile(r"(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})", re.ASCII)
_RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RE_VERSION_PY = re.compile(r"""^VERSION\s*=\s*["']([^"']+)["']\s*$""", re.M)
_CARACTERES_INTERDITS = set('<>:"|?*\\')
_NOMS_RESERVES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_HOTES_LOCAUX = {"127.0.0.1", "localhost", "::1"}


class ErreurMaj(Exception):
    """Refus ou échec de mise à jour ; le message est affichable tel quel."""


# ---------------------------------------------------------------- utilitaires

def cle_version(texte: object) -> tuple[int, int, int] | None:
    """(majeur, mineur, correctif) pour une version X.Y.Z stricte, sinon None."""
    if not isinstance(texte, str):
        return None
    m = _RE_VERSION.fullmatch(texte)
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def chemin_sur(nom: object) -> bool:
    """Chemin relatif POSIX sans remontée, sans lecteur ni nom réservé Windows."""
    if not isinstance(nom, str) or not nom or len(nom) > 240 or nom.startswith("/"):
        return False
    for partie in nom.split("/"):
        if partie in ("", ".", ".."):
            return False
        if any(c in _CARACTERES_INTERDITS or ord(c) < 32 for c in partie):
            return False
        if partie.endswith((" ", ".")):
            return False
        if partie.split(".")[0].lower() in _NOMS_RESERVES:
            return False
    return True


def racine_par_defaut() -> Path:
    """%LOCALAPPDATA%\\AME Tech (même dossier que le lanceur)."""
    explicite = os.environ.get("AMETECH_DONNEES")
    if explicite:
        return Path(explicite)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "AME Tech"
    return Path.home() / ".local" / "share" / "AME Tech"


def charger_cle(chemin: Path) -> tuple[bytes | None, bool]:
    """(clé de 32 octets ou None, clé de test ?)."""
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError:
        return None, False
    est_test = MARQUEUR_CLE_TEST in texte
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        try:
            cle = base64.b64decode(ligne, validate=True)
        except ValueError:
            return None, est_test
        return (cle if len(cle) == 32 else None), est_test
    return None, est_test


def ecrire_atomique(chemin: Path, texte: str) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_name(chemin.name + ".tmp")
    with open(temporaire, "w", encoding="utf-8", newline="\n") as f:
        f.write(texte)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)


def lire_petit_texte(chemin: Path) -> str | None:
    try:
        with open(chemin, "rb") as f:
            return f.read(256).decode("utf-8", "replace").strip() or None
    except OSError:
        return None


def lire_etat_persistant(racine: Path) -> dict:
    try:
        with open(racine / "maj_etat.json", "rb") as f:
            donnees = json.loads(f.read(64 * 1024).decode("utf-8"))
        return donnees if isinstance(donnees, dict) else {}
    except (OSError, ValueError):
        return {}


def ecrire_etat_persistant(racine: Path, donnees: dict) -> None:
    ecrire_atomique(racine / "maj_etat.json", json.dumps(donnees, ensure_ascii=False, indent=1) + "\n")


def analyser_manifeste(brut: bytes) -> dict:
    """Valide la forme du manifeste (déjà authentifié par sa signature)."""
    try:
        m = json.loads(brut.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ErreurMaj("Manifeste illisible.") from exc
    if not isinstance(m, dict) or cle_version(m.get("version")) is None:
        raise ErreurMaj("Manifeste sans version valide.")
    z = m.get("zip")
    if not isinstance(z, dict) or not isinstance(z.get("url"), str) or not _RE_SHA256.match(str(z.get("sha256", ""))):
        raise ErreurMaj("Manifeste sans ZIP valide.")
    fichiers = m.get("fichiers")
    if not isinstance(fichiers, list) or not fichiers or len(fichiers) > FICHIERS_MAX:
        raise ErreurMaj("Manifeste sans liste de fichiers valide.")
    vus = set()
    for f in fichiers:
        if not isinstance(f, dict) or not chemin_sur(f.get("chemin")) or not _RE_SHA256.match(str(f.get("sha256", ""))):
            raise ErreurMaj("Entrée de fichier invalide dans le manifeste.")
        if f["chemin"].lower() in vus:
            raise ErreurMaj("Fichier en double dans le manifeste.")
        vus.add(f["chemin"].lower())
    for champ in ("notes", "date"):
        if champ in m and not isinstance(m[champ], str):
            raise ErreurMaj("Manifeste mal formé.")
    return m


def extraire_zip(chemin_zip: Path, destination: Path, manifeste: dict,
                 taille_max: int = TAILLE_MAX_EXTRAITE) -> set[str]:
    """Extrait en contrôlant chemins, taille, liens et empreintes. Renvoie les membres."""
    attendus = {f["chemin"]: f["sha256"] for f in manifeste["fichiers"]}
    try:
        archive = zipfile.ZipFile(chemin_zip)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ErreurMaj("Archive de mise à jour illisible.") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > FICHIERS_MAX:
            raise ErreurMaj("Archive : trop de fichiers.")
        membres: dict[str, zipfile.ZipInfo] = {}
        vus: set[str] = set()
        total = 0
        for info in infos:
            nom = info.filename
            if info.is_dir():
                if not chemin_sur(nom.rstrip("/")):
                    raise ErreurMaj("Archive : dossier au chemin refusé.")
                continue
            if not chemin_sur(nom):
                raise ErreurMaj(f"Archive : chemin refusé ({nom[:80]!r}).")
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                raise ErreurMaj("Archive : lien symbolique refusé.")
            if nom.lower() in vus:
                raise ErreurMaj("Archive : fichier en double.")
            vus.add(nom.lower())
            total += info.file_size
            if total > taille_max:
                raise ErreurMaj("Archive : contenu trop volumineux.")
            membres[nom] = info
        non_declares = sorted(n for n in membres if n not in attendus)
        if non_declares:
            raise ErreurMaj(f"Archive : fichier absent du manifeste signé ({non_declares[0][:80]}).")
        manquants = [n for n in FICHIERS_OBLIGATOIRES if n not in membres]
        if manquants:
            raise ErreurMaj(f"Archive incomplète : {manquants[0]} manque.")
        base = destination.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        for nom, info in membres.items():
            cible = destination.joinpath(*nom.split("/"))
            if base not in cible.resolve().parents:
                raise ErreurMaj("Archive : chemin hors du dossier de version.")
            cible.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            lus = 0
            with archive.open(info) as source, open(cible, "xb") as sortie:
                while True:
                    bloc = source.read(1024 * 1024)
                    if not bloc:
                        break
                    lus += len(bloc)
                    if lus > info.file_size:
                        raise ErreurMaj("Archive : taille de fichier incohérente.")
                    h.update(bloc)
                    sortie.write(bloc)
            if h.hexdigest() != attendus[nom]:
                raise ErreurMaj(f"Empreinte fausse pour {nom[:80]} : mise à jour refusée.")
    return set(membres)


def _journal(niveau: str, etape: str, message: str) -> None:
    try:
        importlib.import_module(f"{__package__}.journal").log(niveau, etape, message)
    except Exception:  # le journal ne doit jamais casser la mise à jour
        pass


class _Redirection(urllib.request.HTTPRedirectHandler):
    def __init__(self, autorisee):
        super().__init__()
        self._autorisee = autorisee

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._autorisee(newurl):
            raise ErreurMaj("Redirection vers une adresse non autorisée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------- moteur

class Majeur:
    def __init__(self, *, racine: Path | None = None, sources=SOURCES, version_locale: str | None = None,
                 cle: bytes | None = None, chemin_cle: Path | None = None, autoriser_http_local: bool = False,
                 relancer=None, maintenant=time.time, delai_reseau: float = DELAI_RESEAU,
                 taille_max_zip: int = TAILLE_MAX_ZIP):
        self.racine = Path(racine) if racine else racine_par_defaut()
        self.sources = [s if s.endswith("/") else s + "/" for s in sources]
        self.version_locale = version_locale or self._version_installee()
        self.autoriser_http_local = autoriser_http_local
        self.maintenant = maintenant
        self.delai_reseau = delai_reseau
        self.taille_max_zip = taille_max_zip
        self._relancer_injecte = relancer
        self._relais = None
        self._verrou = threading.Lock()
        self._manifeste: dict | None = None
        self._base: str | None = None
        self._derniere_verification = 0.0
        self._raison_cle = None
        if cle is None:
            cle, est_test = charger_cle(chemin_cle or Path(__file__).with_name("cle_publique.txt"))
            if est_test:
                cle, self._raison_cle = None, "Mises à jour désactivées : la clé publique est une clé de test."
            elif cle is None:
                self._raison_cle = "Mises à jour désactivées : clé publique absente ou illisible."
        self._cle = cle
        self._etat = {"version_locale": self.version_locale, "version_dispo": None,
                      "statut": "inconnu", "message": "", "verifie_le": None}
        persistant = lire_etat_persistant(self.racine)
        retour = persistant.get("retour")
        if isinstance(retour, dict) and retour.get("vers") == self.version_locale:
            self._etat.update(statut="erreur", message=(
                f"La version {retour.get('de')} n'a pas démarré correctement : "
                f"retour automatique à la version {self.version_locale}."))
        opener_args = [_Redirection(self._url_autorisee)]
        if autoriser_http_local:
            opener_args.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*opener_args)

    # --- informations

    @staticmethod
    def _version_installee() -> str:
        try:
            return importlib.import_module(f"{__package__}.version").VERSION
        except Exception:
            return "0.0.0"

    def etat(self) -> dict:
        return dict(self._etat)

    def _echecs(self) -> list[str]:
        echecs = lire_etat_persistant(self.racine).get("echecs", [])
        return [e for e in echecs if isinstance(e, str)] if isinstance(echecs, list) else []

    # --- réseau

    def _url_autorisee(self, url: str) -> bool:
        try:
            p = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if p.scheme == "https" and p.hostname:
            return True
        return self.autoriser_http_local and p.scheme == "http" and p.hostname in _HOTES_LOCAUX

    def _lire(self, url: str, limite: int, delai_total: float, sortie=None) -> tuple[bytes | None, str]:
        """Lit `url` (au plus `limite` octets). Renvoie (contenu ou None si `sortie`, sha256)."""
        if not self._url_autorisee(url):
            raise ErreurMaj("Adresse de téléchargement non autorisée.")
        requete = urllib.request.Request(url, headers={
            "User-Agent": f"AME-Tech-maj/{self.version_locale}", "Cache-Control": "no-cache"})
        debut = time.monotonic()
        h = hashlib.sha256()
        morceaux: list[bytes] = []
        total = 0
        with self._opener.open(requete, timeout=self.delai_reseau) as reponse:
            longueur = reponse.headers.get("Content-Length")
            if longueur and longueur.isdigit() and int(longueur) > limite:
                raise ErreurMaj("Fichier distant trop volumineux.")
            # read1 rend ce qui est arrivé (une seule lecture réseau) : le délai total est
            # contrôlé même face à un serveur qui goutte, au pire avec un délai réseau de retard.
            lire_bloc = getattr(reponse, "read1", reponse.read)
            while True:
                if time.monotonic() - debut > delai_total:
                    raise ErreurMaj("Téléchargement trop lent, abandon.")
                bloc = lire_bloc(256 * 1024)
                if not bloc:
                    break
                total += len(bloc)
                if total > limite:
                    raise ErreurMaj("Fichier distant trop volumineux.")
                if time.monotonic() - debut > delai_total:
                    raise ErreurMaj("Téléchargement trop lent, abandon.")
                h.update(bloc)
                if sortie is None:
                    morceaux.append(bloc)
                else:
                    sortie.write(bloc)
        return (b"".join(morceaux) if sortie is None else None), h.hexdigest()

    @staticmethod
    def _hote(url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).hostname or url
        except ValueError:
            return url

    # --- vérification

    def verifier(self) -> dict:
        if not self._verrou.acquire(blocking=False):
            return self.etat()
        try:
            self._verifier()
        finally:
            self._verrou.release()
        return self.etat()

    def _verifier(self) -> None:
        if self._etat["statut"] == "pret":
            return  # relance en cours : rien à revérifier
        self._manifeste = self._base = None
        if self._cle is None:
            self._etat.update(statut="erreur", message=self._raison_cle or "Mises à jour désactivées.")
            return
        erreurs = []
        manifeste = base = None
        for source in self.sources:
            try:
                brut, _ = self._lire(source + NOM_MANIFESTE, TAILLE_MAX_MANIFESTE, DELAI_TOTAL_PETIT)
                sig_txt, _ = self._lire(source + NOM_SIGNATURE, TAILLE_MAX_SIGNATURE, DELAI_TOTAL_PETIT)
                try:
                    signature = base64.b64decode(sig_txt.strip(), validate=True)
                except ValueError as exc:
                    raise ErreurMaj("signature illisible") from exc
                if not ed25519.verifier(self._cle, brut, signature):
                    raise ErreurMaj("signature invalide")
                manifeste, base = analyser_manifeste(brut), source
                break
            except (ErreurMaj, OSError, ValueError, http.client.HTTPException) as exc:
                motif = str(exc) if isinstance(exc, ErreurMaj) else type(exc).__name__
                erreurs.append(f"{self._hote(source)} : {motif}")
                _journal("alerte", "maj", f"Source de mise à jour refusée ({erreurs[-1]})")
        self._derniere_verification = self.maintenant()
        self._etat["verifie_le"] = self._derniere_verification
        if manifeste is None:
            self._etat.update(statut="erreur", message="Aucune source de mise à jour valide : " + " ; ".join(erreurs))
            return
        version = manifeste["version"]
        self._etat["version_dispo"] = version
        locale = cle_version(self.version_locale) or (0, 0, 0)
        if cle_version(version) <= locale:
            self._etat.update(statut="a_jour", message=f"Version {self.version_locale} à jour.")
        elif version in self._echecs():
            self._etat.update(statut="erreur", message=(
                f"La version {version} a déjà échoué au démarrage sur ce poste : elle est ignorée."))
        else:
            self._manifeste, self._base = manifeste, base
            self._etat.update(statut="disponible", message=f"Version {version} disponible.")
            _journal("info", "maj", f"Version {version} disponible ({self._hote(base)})")

    # --- application

    def definir_relais(self, relais) -> None:
        self._relais = relais

    def relais_occupe(self) -> bool:
        """True si le relais tourne (toute phase sauf `arret`) ou si son état est inconnu.

        Seule exception : aucun texte transmis depuis 10 minutes (dernière trame non vide,
        ou, s'il n'a jamais rien transmis, 10 minutes passées dans la même phase).
        """
        if self._relais is None:
            return True
        try:
            etat = self._relais.etat()
        except Exception:
            return True
        r = etat.get("relais") if isinstance(etat, dict) and isinstance(etat.get("relais"), dict) else etat
        if not isinstance(r, dict):
            return True
        if r.get("phase") == "arret":
            return False

        def instant(valeur):
            return valeur if isinstance(valeur, (int, float)) and not isinstance(valeur, bool) else None

        repere = instant(r.get("dernier_envoi"))
        if repere is None:
            repere = instant(r.get("phase_depuis"))
        if repere is not None:
            return self.maintenant() - repere < INACTIVITE_AVANT_MAJ
        return True

    def _relance_possible(self) -> bool:
        return self._relancer_injecte is not None or (sys.platform == "win32" and bool(os.environ.get("AMETECH_LANCEUR")))

    def appliquer(self) -> dict:
        if not self._verrou.acquire(blocking=False):
            etat = self.etat()
            etat["message"] = "Une opération de mise à jour est déjà en cours."
            return etat
        try:
            if self._etat["statut"] == "pret":
                return self.etat()  # déjà installée, relance en cours
            if self._manifeste is None:
                self._verifier()
            if self._manifeste is None:
                return self.etat()
            if self.relais_occupe():
                self._etat["message"] = "Mise à jour reportée : le relais transmet en ce moment ou se reconnecte."
                return self.etat()
            if not self._relance_possible():
                self._etat["message"] = "Mise à jour possible seulement quand AME Tech est lancé par AME Tech.exe."
                return self.etat()
            version = self._manifeste["version"]
            self._etat.update(statut="telechargement", message=f"Téléchargement de la version {version}.")
            self._installer(self._manifeste, self._base or self.sources[0])
            self._etat.update(statut="pret", message=f"Redémarrage sur la version {version}.")
            _journal("info", "maj", f"Version {version} installée, redémarrage")
            (self._relancer_injecte or self._relancer_par_lanceur)(self.version_locale, version)
            self._manifeste = None
        except (ErreurMaj, OSError, ValueError, http.client.HTTPException) as exc:
            motif = str(exc) if isinstance(exc, ErreurMaj) else f"{type(exc).__name__} : {exc}"
            self._manifeste = None
            self._etat.update(statut="erreur", message=f"Mise à jour refusée : {motif}")
            _journal("erreur", "maj", self._etat["message"])
        finally:
            self._verrou.release()
        return self.etat()

    def _installer(self, manifeste: dict, base: str) -> None:
        version = manifeste["version"]
        versions = self.racine / "versions"
        telechargements = self.racine / "telechargements"
        versions.mkdir(parents=True, exist_ok=True)
        telechargements.mkdir(parents=True, exist_ok=True)
        partiel = telechargements / f"{version}.zip.part"
        candidats = []
        for url in (manifeste["zip"]["url"], base + NOM_ZIP):
            if url not in candidats:
                candidats.append(url)
        erreurs = []
        for url in candidats:
            try:
                with open(partiel, "wb") as sortie:
                    _, empreinte = self._lire(url, self.taille_max_zip, DELAI_TOTAL_ZIP, sortie)
                if empreinte != manifeste["zip"]["sha256"]:
                    raise ErreurMaj("empreinte du ZIP fausse")
                break
            except (ErreurMaj, OSError, ValueError, http.client.HTTPException) as exc:
                erreurs.append(f"{self._hote(url)} : {exc if isinstance(exc, ErreurMaj) else type(exc).__name__}")
                partiel.unlink(missing_ok=True)
        else:
            raise ErreurMaj("ZIP refusé (" + " ; ".join(erreurs) + ")")
        temporaire = versions / f".{version}.extraction"
        shutil.rmtree(temporaire, ignore_errors=True)
        try:
            extraire_zip(partiel, temporaire, manifeste)
            texte = (temporaire / "ametech" / "version.py").read_text(encoding="utf-8")
            trouve = _RE_VERSION_PY.search(texte)
            if not trouve or trouve.group(1) != version:
                raise ErreurMaj("La version du code ne correspond pas au manifeste.")
            final = versions / version
            if final.exists():
                shutil.rmtree(final)
            os.replace(temporaire, final)
        except BaseException:
            shutil.rmtree(temporaire, ignore_errors=True)
            raise
        finally:
            partiel.unlink(missing_ok=True)
        ecrire_atomique(self.racine / "precedente.txt", self.version_locale + "\n")
        ecrire_atomique(self.racine / "courante.txt", version + "\n")

    def _relancer_par_lanceur(self, ancienne: str, nouvelle: str) -> None:
        commande = [sys.executable, "-I", "-m", f"{__package__}.maj", "--garde",
                    "--racine", str(self.racine), "--ancienne", ancienne, "--nouvelle", nouvelle]
        options = {"cwd": os.environ.get("AMETECH_RACINE") or str(Path(__file__).resolve().parents[1]),
                   "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                   "close_fds": True}
        if sys.platform == "win32":
            options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            subprocess.Popen(commande, **options)
        except OSError as exc:
            ecrire_atomique(self.racine / "courante.txt", ancienne + "\n")
            raise ErreurMaj("Impossible de lancer la garde de retour arrière.") from exc

        def sortir() -> None:
            time.sleep(1.5)
            fonction = _arret
            try:
                if fonction is not None:
                    fonction(CODE_RELANCE)
            finally:
                os._exit(CODE_RELANCE)

        threading.Thread(target=sortir, name="maj-relance", daemon=True).start()

    # --- boucle de fond

    def boucle(self, relais, arret: threading.Event | None = None, premier_delai: float = 20.0) -> None:
        """Vérifie au lancement puis toutes les 6 h ; applique dès que le relais le permet."""
        self._relais = relais
        arret = arret or threading.Event()
        attente = premier_delai
        while not arret.wait(attente):
            try:
                if self._manifeste is None or self.maintenant() - self._derniere_verification >= INTERVALLE:
                    self.verifier()
                if self._etat["statut"] == "disponible":
                    self.appliquer()
            except Exception as exc:  # la boucle ne doit jamais mourir
                _journal("erreur", "maj", f"Boucle de mise à jour : {type(exc).__name__}")
            attente = REESSAI_OCCUPE if self._etat["statut"] == "disponible" else INTERVALLE


# ---------------------------------------------------------------- garde

def sonde_http(port: int, delai: float = 1.5) -> str | None:
    """Version annoncée par http://127.0.0.1:<port>/api/etat, ou None."""
    try:
        connexion = http.client.HTTPConnection("127.0.0.1", port, timeout=delai)
        try:
            connexion.request("GET", "/api/etat", headers={"Cache-Control": "no-store"})
            reponse = connexion.getresponse()
            if reponse.status != 200:
                return None
            donnees = json.loads(reponse.read(256 * 1024).decode("utf-8"))
        finally:
            connexion.close()
        version = donnees.get("version") if isinstance(donnees, dict) else None
        return version if isinstance(version, str) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def tuer_enfant(racine: Path, nouvelle: str) -> bool:
    """Arrête le processus Python lancé par le lanceur s'il exécute bien `nouvelle`."""
    if sys.platform != "win32":
        return False
    texte = lire_petit_texte(racine / "enfant.pid")
    if not texte or not texte.isdigit() or int(texte) == os.getpid():
        return False
    import ctypes
    from ctypes import wintypes
    noyau = ctypes.WinDLL("kernel32", use_last_error=True)
    noyau.OpenProcess.restype = wintypes.HANDLE
    noyau.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    noyau.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
    noyau.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    noyau.CloseHandle.argtypes = (wintypes.HANDLE,)
    poignee = noyau.OpenProcess(0x0001 | 0x1000, False, int(texte))  # TERMINATE | QUERY_LIMITED_INFORMATION
    if not poignee:
        return False
    try:
        tampon = ctypes.create_unicode_buffer(32768)
        taille = wintypes.DWORD(len(tampon))
        if not noyau.QueryFullProcessImageNameW(poignee, 0, tampon, ctypes.byref(taille)):
            return False
        attendu = str((racine / "versions" / nouvelle).resolve()).lower() + "\\"
        if not tampon.value.lower().startswith(attendu):
            return False  # PID réattribué à un autre programme : on n'y touche pas
        return bool(noyau.TerminateProcess(poignee, CODE_RELANCE))
    finally:
        noyau.CloseHandle(poignee)


def nettoyer_versions(racine: Path, garder: set[str]) -> None:
    versions = racine / "versions"
    if not versions.is_dir():
        return
    for dossier in versions.iterdir():
        if dossier.is_dir() and dossier.name not in garder and (cle_version(dossier.name) or dossier.name.startswith(".")):
            shutil.rmtree(dossier, ignore_errors=True)


def garder(racine: Path, ancienne: str, nouvelle: str, *, delai: float = DELAI_SANTE, ports=PORTS_UI,
           sonde=sonde_http, tuer=tuer_enfant, pause: float = 0.5, horloge=time.monotonic) -> bool:
    """Attend que `nouvelle` réponde ; sinon remet `ancienne`. True si la nouvelle version tient."""
    racine = Path(racine)
    fin = horloge() + delai
    while horloge() < fin:
        if any(sonde(port) == nouvelle for port in ports):
            persistant = lire_etat_persistant(racine)
            persistant.pop("retour", None)
            persistant["derniere_reussite"] = {"version": nouvelle, "t": time.time()}
            ecrire_etat_persistant(racine, persistant)
            nettoyer_versions(racine, {nouvelle, ancienne})
            return True
        time.sleep(pause)
    if lire_petit_texte(racine / "courante.txt") == nouvelle:
        ecrire_atomique(racine / "courante.txt", ancienne + "\n")
    persistant = lire_etat_persistant(racine)
    echecs = [e for e in persistant.get("echecs", []) if isinstance(e, str)] if isinstance(persistant.get("echecs"), list) else []
    if nouvelle not in echecs:
        echecs.append(nouvelle)
    persistant["echecs"] = echecs[-20:]
    persistant["retour"] = {"de": nouvelle, "vers": ancienne, "t": time.time()}
    ecrire_etat_persistant(racine, persistant)
    try:
        tuer(racine, nouvelle)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- API du module

_defaut: Majeur | None = None
_arret = None
_verrou_defaut = threading.Lock()


def _instance() -> Majeur:
    global _defaut
    with _verrou_defaut:
        if _defaut is None:
            _defaut = Majeur()
        return _defaut


def etat() -> dict:
    return _instance().etat()


def verifier() -> dict:
    return _instance().verifier()


def appliquer() -> dict:
    return _instance().appliquer()


def boucle(relais, arret: threading.Event | None = None) -> None:
    _instance().boucle(relais, arret)


def definir_relais(relais) -> None:
    _instance().definir_relais(relais)


def definir_arret(fonction) -> None:
    """fonction(code) : arrêt propre de l'application puis os._exit(code)."""
    global _arret
    _arret = fonction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ametech.maj", description="Garde de mise à jour AME Tech")
    parser.add_argument("--garde", action="store_true", required=True)
    parser.add_argument("--racine", required=True)
    parser.add_argument("--ancienne", required=True)
    parser.add_argument("--nouvelle", required=True)
    args = parser.parse_args(argv)
    if cle_version(args.nouvelle) is None or cle_version(args.ancienne) is None:
        return 2
    return 0 if garder(Path(args.racine), args.ancienne, args.nouvelle) else 1


if __name__ == "__main__":
    sys.exit(main())
