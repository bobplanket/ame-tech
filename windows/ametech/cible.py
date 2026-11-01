"""Client du serveur cible (tech.ame.paris ou secours) avec connexion par code TOTP.

`POST /api/login {"code"}` avec le code du moment, puis cookie `badaboum_session`
(12 h). Nouveau login avant l'expiration ou sur un 401. Le code est calculé à
l'heure du serveur quand l'écart d'horloge est connu, ce qui absorbe un PC mal
réglé. Ni le secret, ni le code, ni le cookie ne sortent dans le journal.
"""
from __future__ import annotations
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ametech import journal, totp

MAX_REPONSE = 1_000_000
MARGE_COOKIE = 300  # secondes : on se reconnecte 5 min avant l'expiration du cookie


class ErreurCible(Exception):
    def __init__(self, message: str, statut: int = 0, raison: str = ""):
        super().__init__(message)
        self.statut = statut
        self.raison = raison


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def valider_url(url: str) -> str:
    """HTTPS obligatoire, sauf boucle locale (tests). Adresse de base seulement."""
    url = str(url or "").strip().rstrip("/")
    p = urllib.parse.urlsplit(url)
    local = p.hostname in {"localhost", "127.0.0.1", "::1"}
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("Adresse de serveur invalide")
    if p.scheme != "https" and not (p.scheme == "http" and local):
        raise ValueError("HTTPS est requis, sauf sur cette machine")
    if not p.hostname or p.path not in {"", "/"}:
        raise ValueError("Indiquer seulement l'adresse du serveur")
    return url


class Cible:
    def __init__(self, url: str, fournisseur_secret, *, delai: float = 6.0):
        self.url = valider_url(url)
        self.fournisseur_secret = fournisseur_secret  # callable -> secret base32 ou ""
        self.delai = delai
        self.ecart_s: float | None = None  # heure serveur - heure locale
        self._cookie = ""
        self._expire = 0.0
        self._verrou = threading.RLock()
        handlers = [_SansRedirection()]
        if urllib.parse.urlsplit(self.url).hostname in {"localhost", "127.0.0.1", "::1"}:
            handlers.append(urllib.request.ProxyHandler({}))
        self._ouvreur = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------ bas niveau
    def _http(self, methode: str, chemin: str, corps=None, entetes=None):
        donnees = None if corps is None else json.dumps(corps, ensure_ascii=False).encode()
        h = {"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "AME-Tech"}
        if donnees is not None:
            h["Content-Type"] = "application/json"
        h.update(entetes or {})
        req = urllib.request.Request(self.url + chemin, data=donnees, headers=h, method=methode)
        try:
            with self._ouvreur.open(req, timeout=self.delai) as r:
                brut = r.read(MAX_REPONSE + 1)
                statut, en_tetes = r.status, r.headers
        except urllib.error.HTTPError as exc:
            try:
                brut = exc.read(8192)
            except OSError:
                brut = b""
            statut, en_tetes = exc.code, exc.headers
            exc.close()
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
            cause = getattr(exc, "reason", exc)
            if isinstance(cause, (TimeoutError, socket.timeout)):
                raise ErreurCible("Délai de réponse dépassé") from None
            if isinstance(cause, socket.gaierror):
                raise ErreurCible("Nom de serveur introuvable (Internet ou DNS)") from None
            if isinstance(cause, ConnectionRefusedError):
                raise ErreurCible("Connexion refusée par le serveur") from None
            raise ErreurCible("Connexion impossible ou interrompue") from None
        if len(brut) > MAX_REPONSE:
            raise ErreurCible("Réponse trop volumineuse")
        try:
            valeur = json.loads(brut) if brut.strip() else {}
        except ValueError:
            valeur = {}
        return statut, valeur, en_tetes

    # ------------------------------------------------------------ horloge
    def heure(self) -> float:
        """Mesure l'écart heure serveur - heure locale via GET /api/heure (route publique)."""
        avant = time.time()
        statut, valeur, _ = self._http("GET", "/api/heure")
        apres = time.time()
        if statut != 200 or not isinstance(valeur, dict) or not isinstance(valeur.get("t"), (int, float)):
            raise ErreurCible("Heure du serveur indisponible (HTTP " + str(statut) + ")", statut)
        self.ecart_s = float(valeur["t"]) - (avant + apres) / 2
        return self.ecart_s

    def code_du_moment(self) -> str:
        secret = self.fournisseur_secret() or ""
        if not secret:
            raise ErreurCible("Poste non enrôlé : coller le code d'enrôlement", 0, "non_enrole")
        journal.enregistrer_sensible(secret)
        return totp.code(secret, time.time() + (self.ecart_s or 0.0))

    # ------------------------------------------------------------ session
    def connecte(self) -> bool:
        with self._verrou:
            return bool(self._cookie) and time.time() < self._expire - MARGE_COOKIE

    def oublier_session(self) -> None:
        with self._verrou:
            self._cookie, self._expire = "", 0.0

    def login(self) -> None:
        if self.ecart_s is None:
            try:
                self.heure()
            except ErreurCible:
                pass  # le login dira mieux ce qui ne va pas
        code = self.code_du_moment()
        statut, valeur, en_tetes = self._http("POST", "/api/login", {"code": code})
        if statut == 429:
            raise ErreurCible("Trop d'essais de code : le serveur bloque 15 minutes", 429, "bloque")
        if statut == 401:
            raise ErreurCible("Code refusé par le serveur : vérifier l'heure du PC ou réenrôler le poste", 401, "code_refuse")
        if statut != 200:
            raise ErreurCible("Connexion refusée (HTTP " + str(statut) + ")", statut)
        cookie, duree = "", 43200
        for ligne in en_tetes.get_all("Set-Cookie") or []:
            premier, *attributs = [x.strip() for x in ligne.split(";")]
            nom, _, valeur_cookie = premier.partition("=")
            if nom == "badaboum_session" and valeur_cookie:
                cookie = valeur_cookie
                for a in attributs:
                    k, _, v = a.partition("=")
                    if k.lower() == "max-age" and v.isdigit():
                        duree = int(v)
        if not cookie:
            raise ErreurCible("Le serveur n'a pas ouvert de session", statut)
        journal.enregistrer_sensible(cookie)
        with self._verrou:
            self._cookie, self._expire = cookie, time.time() + duree
        journal.log("info", "connexion", "Session ouverte sur " + self.url)

    def requete(self, methode: str, chemin: str, corps=None):
        """Requête authentifiée. Login si besoin, et un seul nouvel essai après un 401."""
        for essai in range(2):
            if not self.connecte():
                self.login()
            with self._verrou:
                cookie = self._cookie
            statut, valeur, _ = self._http(methode, chemin, corps, {"Cookie": "badaboum_session=" + cookie})
            if statut == 401 and essai == 0:
                journal.log("alerte", "connexion", "Session refusée (401) par " + self.url + " : nouvelle connexion avec le code du moment")
                self.oublier_session()
                continue
            if statut == 401:
                raise ErreurCible("Session refusée par le serveur", 401, "code_refuse")
            if statut == 429:
                raise ErreurCible("Trop d'essais de code : le serveur bloque 15 minutes", 429, "bloque")
            if statut >= 400:
                raison = str(valeur.get("erreur", "")) if isinstance(valeur, dict) else ""
                raise ErreurCible("Réponse HTTP " + str(statut) + (" (" + raison + ")" if raison else ""), statut, raison)
            return valeur
        raise ErreurCible("Session refusée par le serveur", 401, "code_refuse")

    def session_publique(self) -> dict:
        statut, valeur, _ = self._http("GET", "/api/session")
        if statut != 200 or not isinstance(valeur, dict) or "pin_requis" not in valeur:
            raise ErreurCible("Régie non reconnue (HTTP " + str(statut) + ")", statut)
        return valeur
