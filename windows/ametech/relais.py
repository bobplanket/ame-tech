"""Relais ProPresenter vers le serveur cible, avec redondance et reconnexion.

Deux fils :
- source : trouve ProPresenter (port choisi, dernier port, 50001, ports du
  processus), lit le flux /v1/status/updates, repli en lecture HTTP bornée ;
- cible : connexion TOTP, `claim` puis une trame toutes les 2 s ou à chaque
  changement. Le serveur éteint le texte si plus rien n'arrive (bail de 9 s).

Jamais de file d'attente : chaque trame porte le texte du moment, et un texte
dont la source n'a pas donné signe de vie depuis 8 s devient « rien ».

Mode Auto / Manuel : c'est la source du serveur (`presenter` ou `manual`).
En Manuel le relais garde sa session et envoie des trames vides. Un choix fait
dans le logiciel est poussé au serveur ; un choix fait ailleurs (régie,
panneau OBS) remonte par la réponse des trames.
"""
from __future__ import annotations
import random
import socket
import threading
import time
import urllib.parse

from ametech import cible as cible_mod
from ametech import coffre, journal, propresenter as pp, totp
from ametech.propresenter import Contenu, ErreurReseau
from ametech.cible import ErreurCible

PHASES = ("arret", "recherche_pp", "connexion", "en_ligne", "erreur")


class Relais:
    ECHECS_BASCULE = 3          # échecs consécutifs avant de passer à la cible suivante
    RETOUR_PREMIERE_S = 300.0   # essai de retour sur la première cible
    DELAI_MAX = 15.0            # reconnexion progressive : 1, 2, 4, 8, 15 s
    BATTEMENT_S = 2.0           # trame de maintien
    FRAICHEUR_S = 8.0           # texte périmé si ProPresenter se tait plus longtemps
    TOLERANCE_HORLOGE_S = 20.0
    ATTENTE_CODE_REFUSE = 35.0  # attendre le code suivant, sans épuiser les essais autorisés
    ATTENTE_BLOQUE = 300.0
    ATTENTE_OCCUPE = 12.0
    INTERVALLE_HORLOGE = 120.0

    def __init__(self, lire_reglages=None, ecrire_reglages=None, nom_poste: str | None = None):
        self._lire = lire_reglages or coffre.lire
        self._ecrire = ecrire_reglages or coffre.ecrire
        self.nom_poste = nom_poste or socket.gethostname() or "AME Tech"
        self._verrou = threading.RLock()
        self._arret = threading.Event()
        self._reveil = threading.Event()
        self._fils: list[threading.Thread] = []
        self._generation = 0
        self._reponse_flux = None
        self._clients: dict[str, cible_mod.Cible] = {}
        reg = self._reglages()
        self.client_id = reg.get("client_id") or "ametech"
        self.mode = reg.get("mode", "auto")
        self._mode_a_pousser: str | None = None
        self.actif = False
        # source
        self._contenu = Contenu()
        self._source_vue = 0.0
        self._pp: dict | None = None
        self._dernier_port = 0
        self._transport = ""
        self._erreur_pp = ""
        # cible
        self._idx = 0
        self._bascule_t = 0.0
        self._connectee = False
        self._erreur_cible = ""
        self._erreur_fatale = ""
        self._source_serveur = ""
        # affichage
        self._dernier_texte = ""
        self._depuis: float | None = None
        self._dernier_envoi: float | None = None  # dernière trame portant un texte non vide
        self._phase = "arret"
        self._phase_depuis = time.time()
        self._message = "Relais à l'arrêt"
        # horloge
        self._ecart: float | None = None
        self._horloge_t = 0.0
        self._horloge_en_cours = False
        self._stats = {"trames": 0, "logins": 0, "bascules": 0}

    # ------------------------------------------------------------ réglages
    def _reglages(self) -> dict:
        try:
            return self._lire()
        except Exception as exc:
            journal.log("erreur", "reglages", "Lecture des réglages impossible : " + type(exc).__name__)
            return dict(coffre.DEFAUTS)

    def _secret(self) -> str:
        return self._reglages().get("secret_totp") or ""

    def _cibles(self) -> list[str]:
        sortie = []
        for url in self._reglages().get("cibles") or []:
            try:
                sortie.append(cible_mod.valider_url(url))
            except ValueError:
                journal.log("alerte", "reglages", "Adresse de serveur ignorée : " + str(url)[:100])
        return sortie or ["https://tech.ame.paris"]

    def _client(self, url: str) -> cible_mod.Cible:
        with self._verrou:
            c = self._clients.get(url)
            if c is None:
                c = self._clients[url] = cible_mod.Cible(url, self._secret)
                if self._ecart is not None:
                    c.ecart_s = self._ecart
            return c

    # ------------------------------------------------------------ cycle de vie
    def demarrer(self) -> None:
        self.arreter(journaliser=False)
        with self._verrou:
            self._arret = threading.Event()
            self._reveil = threading.Event()
            self._generation += 1
            gen = self._generation
            self.actif = True
            self._erreur_fatale = self._erreur_cible = self._erreur_pp = ""
            self._mode_a_pousser = None
            self._idx, self._bascule_t = 0, 0.0
            self._fils = [threading.Thread(target=self._boucle_source, args=(self._arret, gen), name="ametech-source", daemon=True),
                          threading.Thread(target=self._boucle_cible, args=(self._arret, gen), name="ametech-cible", daemon=True)]
        journal.log("info", "relais", "Démarrage du relais ProPresenter")
        for f in self._fils:
            f.start()
        self._maj_phase()

    def arreter(self, journaliser: bool = True) -> None:
        with self._verrou:
            etait_actif = self.actif
            self._arret.set()
            self._reveil.set()
            self._generation += 1
            reponse, self._reponse_flux = self._reponse_flux, None
            fils, self._fils = self._fils, []
        if reponse is not None:
            try:
                reponse.close()
            except OSError:
                pass
        for f in fils:
            if f is not threading.current_thread():
                f.join(timeout=8)
        with self._verrou:
            self.actif = False
            self._contenu = Contenu()
            self._pp = None
            self._connectee = False
            self._transport = ""
        if journaliser and etait_actif:
            journal.log("info", "relais", "Relais arrêté")
        self._maj_phase()

    def definir_mode(self, mode: str) -> None:
        if mode not in ("auto", "manuel"):
            raise ValueError("Mode inconnu : auto ou manuel")
        with self._verrou:
            self.mode = mode
            if self.actif:
                self._mode_a_pousser = mode
        try:
            self._ecrire({"mode": mode})
        except Exception as exc:
            journal.log("alerte", "reglages", "Mode non mémorisé : " + type(exc).__name__)
        journal.log("info", "mode", "Mode " + ("Auto : le relais envoie les textes" if mode == "auto" else "Manuel : le relais reste connecté sans envoyer de texte"))
        self._reveil.set()

    # ------------------------------------------------------------ source ProPresenter
    def _poser_contenu(self, contenu: Contenu, gen: int) -> None:
        with self._verrou:
            if gen != self._generation:
                return
            change = contenu != self._contenu
            self._contenu = contenu
        if change:
            self._reveil.set()

    def _vu(self, gen: int) -> None:
        with self._verrou:
            if gen == self._generation:
                self._source_vue = time.monotonic()

    def _instantane(self, port: int, gen: int):
        actif, courante = pp.lire_instantane(port)
        self._poser_contenu(pp.contenu_depuis({"active": actif, "text": courante.get("text") or "", "notes": courante.get("notes") or ""}), gen)
        self._vu(gen)
        return actif, courante

    def _boucle_source(self, arret: threading.Event, gen: int) -> None:
        delai = 1.0
        while not arret.is_set():
            self._poser_contenu(Contenu(), gen)
            reponse, etape = None, "Recherche de ProPresenter"
            try:
                port, description = pp.decouvrir(int(self._reglages().get("port_pp") or 0), self._dernier_port)
                if arret.is_set():
                    break
                nouveau = port != self._dernier_port or self._pp is None
                self._dernier_port = port
                with self._verrou:
                    self._pp = {"port": port, "version": pp.version_depuis(description), "nom": description}
                    self._erreur_pp = ""
                if nouveau:
                    journal.log("info", "propresenter", description + " trouvé sur le port " + str(port))
                self._maj_phase()
                actif, courante, instantane_ok = False, {}, True
                etape = "Lecture initiale GET /v1/status/layers et /v1/status/slide"
                try:
                    actif, courante = self._instantane(port, gen)
                except ErreurReseau as exc:
                    if exc.statut != 404:
                        raise
                    instantane_ok = False  # certaines versions n'ont que le flux agrégé
                chemins = pp.CHEMINS_STATUT if instantane_ok else [c for c in pp.CHEMINS_STATUT if c != "presentation/slide_index"]
                etape = "Ouverture POST /v1/status/updates"
                try:
                    reponse = pp.ouvrir_flux(port, chemins)
                except ErreurReseau as exc:
                    if not instantane_ok:
                        raise
                    journal.log("alerte", "propresenter", "Flux indisponible (" + str(exc) + ") : lecture HTTP compatible")
                    etape = "Lecture HTTP compatible"
                    self._transport = "HTTP compatible"
                    while not arret.is_set():
                        self._instantane(port, gen)
                        delai = 1.0
                        if arret.wait(0.75):
                            break
                    continue
                with self._verrou:
                    if arret.is_set():
                        reponse.close()
                        break
                    self._reponse_flux = reponse
                    self._transport = "Flux natif"
                journal.log("info", "propresenter", "Flux natif ouvert (/v1/status/updates)")
                delai, compte, efface, diapo_a = 1.0, 0, -1, 0
                etape = "Lecture du flux ProPresenter"
                for env in pp.objets_flux(pp.morceaux(reponse, arret)):
                    if arret.is_set():
                        break
                    compte += 1
                    self._vu(gen)
                    chemin = str(env.get("url", "")).lstrip("/").removeprefix("v1/")
                    donnees = env.get("data")
                    if chemin == "status/layers":
                        actif = isinstance(donnees, dict) and donnees.get("slide") is True
                        if not actif:
                            efface = compte
                            self._poser_contenu(Contenu(), gen)
                        elif diapo_a > efface:
                            self._poser_contenu(pp.contenu_depuis({"active": True, "text": courante.get("text") or "", "notes": courante.get("notes") or ""}), gen)
                    elif chemin == "status/slide":
                        courante = (donnees.get("current") or {}) if isinstance(donnees, dict) else {}
                        if not isinstance(courante, dict):
                            courante = {}
                        diapo_a = compte
                        self._poser_contenu(pp.contenu_depuis({"active": actif, "text": courante.get("text") or "", "notes": courante.get("notes") or ""}), gen)
                    elif chemin == "presentation/slide_index":
                        try:
                            actif, courante = self._instantane(port, gen)
                        except ErreurReseau as exc:
                            if exc.statut != 404:
                                raise
                            self._poser_contenu(Contenu(), gen)
                        diapo_a = compte
            except Exception as exc:
                if gen == self._generation and not arret.is_set():
                    detail = str(exc) if isinstance(exc, ErreurReseau) else "Réponse inattendue (" + type(exc).__name__ + ")"
                    with self._verrou:
                        perdu = self._pp is not None
                        self._erreur_pp = etape + " : " + detail
                        if etape == "Recherche de ProPresenter":
                            self._pp = None
                        self._transport = ""
                    journal.log("alerte" if perdu else "info", "propresenter", self._erreur_pp)
            finally:
                self._poser_contenu(Contenu(), gen)
                if reponse is not None:
                    try:
                        reponse.close()
                    except OSError:
                        pass
                    with self._verrou:
                        if self._reponse_flux is reponse:
                            self._reponse_flux = None
                self._maj_phase()
            if arret.wait(delai * random.uniform(.85, 1.15)):
                break
            delai = min(self.DELAI_MAX, delai * 2)

    # ------------------------------------------------------------ horloge
    def _noter_horloge(self, ecart: float) -> None:
        with self._verrou:
            ancien = self._ecart
            self._ecart, self._horloge_t = ecart, time.monotonic()
            for c in self._clients.values():
                c.ecart_s = ecart
        if abs(ecart) > self.TOLERANCE_HORLOGE_S and (ancien is None or abs(ancien) <= self.TOLERANCE_HORLOGE_S):
            journal.log("alerte", "horloge", "Horloge du PC décalée de %.0f s par rapport au serveur : régler l'heure de Windows" % ecart)

    def verifier_horloge(self) -> float | None:
        try:
            ecart = self._client(self.url_cible()).heure()
        except (ErreurCible, ValueError):
            return None
        self._noter_horloge(ecart)
        return ecart

    def _horloge_en_fond(self) -> None:
        with self._verrou:
            if self._horloge_en_cours or (self._horloge_t and time.monotonic() - self._horloge_t < self.INTERVALLE_HORLOGE):
                return
            self._horloge_en_cours = True

        def tache():
            try:
                self.verifier_horloge()
            finally:
                with self._verrou:
                    self._horloge_en_cours = False
                    if not self._horloge_t:
                        self._horloge_t = time.monotonic()  # pas de rafale si hors ligne
        threading.Thread(target=tache, name="ametech-horloge", daemon=True).start()

    def code(self) -> dict:
        """Code du moment, calculé à l'heure du serveur si l'écart est connu."""
        secret = self._secret()
        if not secret:
            raise PermissionError("Poste non enrôlé")
        journal.enregistrer_sensible(secret)
        t = time.time() + (self._ecart or 0.0)
        return {"code": totp.code(secret, t), "reste_s": int(totp.PAS - (t % totp.PAS)) or totp.PAS}

    # ------------------------------------------------------------ cible
    def _contenu_a_envoyer(self) -> Contenu:
        with self._verrou:
            if self.mode != "auto":
                return Contenu()
            if time.monotonic() - self._source_vue >= self.FRAICHEUR_S:
                return Contenu()
            return self._contenu

    def _trame(self, c: cible_mod.Cible, session: str, sequence: int, contenu: Contenu) -> dict:
        ecart = c.ecart_s or 0.0
        return c.requete("POST", "/api/presenter/frame", {"session": session, "sequence": sequence,
                                                          "sent_at": time.time() + ecart, "content": contenu.dict()})

    def _liberer(self, c: cible_mod.Cible, session: str | None, sequence: int) -> None:
        """Trame vide en partant : le texte disparaît tout de suite au lieu d'attendre la fin du bail."""
        if not session:
            return
        try:
            self._trame(c, session, sequence + 1, Contenu())
        except (ErreurCible, ValueError):
            pass

    def _boucle_cible(self, arret: threading.Event, gen: int) -> None:
        session, sequence, delai, echecs = None, 0, 1.0, 0
        c = None
        while not arret.is_set():
            cibles = self._cibles()
            with self._verrou:
                if self._idx >= len(cibles):
                    self._idx = 0
                idx = self._idx
            url = cibles[idx]
            c = self._client(url)
            if not self._secret():
                self._erreur_fatale = "Poste non enrôlé : coller le code d'enrôlement donné par l'admin"
                self._connectee = False
                self._maj_phase()
                arret.wait(2)
                continue
            # Retour vers la cible principale quand elle répond de nouveau.
            if idx > 0 and time.monotonic() - self._bascule_t >= self.RETOUR_PREMIERE_S:
                try:
                    self._client(cibles[0]).heure()
                    journal.log("info", "redondance", "Retour sur " + cibles[0] + " qui répond de nouveau")
                    self._liberer(c, session, sequence)
                    with self._verrou:
                        self._idx, idx = 0, 0
                    session, echecs, delai = None, 0, 1.0
                    url, c = cibles[0], self._client(cibles[0])
                except ErreurCible:
                    self._bascule_t = time.monotonic()
            etape = "connexion"
            try:
                if session is None:
                    etape = "GET /api/heure"
                    try:
                        self._noter_horloge(c.heure())
                    except ErreurCible:
                        if c.ecart_s is None:
                            raise
                    etape = "POST /api/login"
                    if not c.connecte():
                        c.login()
                        self._stats["logins"] += 1
                    etape = "POST /api/presenter/claim"
                    reponse = c.requete("POST", "/api/presenter/claim", {"name": self.nom_poste, "client_id": self.client_id})
                    if not isinstance(reponse, dict) or not isinstance(reponse.get("session"), str):
                        raise ErreurCible("Réponse de claim invalide")
                    if isinstance(reponse.get("server_time"), (int, float)) and c.ecart_s is None:
                        c.ecart_s = float(reponse["server_time"]) - time.time()
                    session, sequence = reponse["session"], 0
                    journal.enregistrer_sensible(session)
                    journal.log("info", "connexion", "Relais connecté à " + url)
                with self._verrou:
                    pousser = self._mode_a_pousser
                if pousser:
                    etape = "POST /api/etat (source)"
                    c.requete("POST", "/api/etat", {"source": "presenter" if pousser == "auto" else "manual"})
                    with self._verrou:
                        if self._mode_a_pousser == pousser:
                            self._mode_a_pousser = None
                    journal.log("info", "mode", "Serveur passé en " + ("Auto (ProPresenter)" if pousser == "auto" else "Manuel (régie)"))
                if arret.is_set() or gen != self._generation:
                    break
                contenu = self._contenu_a_envoyer()
                sequence += 1
                etape = "POST /api/presenter/frame"
                reponse = self._trame(c, session, sequence, contenu)
                self._stats["trames"] += 1
                with self._verrou:
                    if gen != self._generation:
                        break
                    if not self._connectee:
                        delai = 1.0
                    self._connectee, self._erreur_cible, self._erreur_fatale = True, "", ""
                    echecs = 0
                    texte = contenu.texte
                    if texte.strip():
                        self._dernier_envoi = time.time()
                    if texte != self._dernier_texte:
                        self._dernier_texte, self._depuis = texte, time.time()
                    source = reponse.get("source") if isinstance(reponse, dict) else None
                    if source in ("presenter", "manual"):
                        self._source_serveur = source
                        voulu = "auto" if source == "presenter" else "manuel"
                        if self._mode_a_pousser is None and voulu != self.mode:
                            self.mode = voulu
                            journal.log("info", "mode", "Mode " + ("Auto" if voulu == "auto" else "Manuel") + " choisi depuis la régie")
                delai = 1.0
                self._maj_phase()
                if time.monotonic() - self._horloge_t >= self.INTERVALLE_HORLOGE:
                    try:
                        self._noter_horloge(c.heure())
                    except ErreurCible:
                        pass
                self._reveil.wait(self.BATTEMENT_S)
                self._reveil.clear()
                continue
            except (ErreurCible, ValueError) as exc:
                if arret.is_set() or gen != self._generation:
                    break
                attente = None
                raison = getattr(exc, "raison", "")
                message = etape + " sur " + url + " : " + str(exc)
                with self._verrou:
                    self._connectee = False
                    self._erreur_cible = message
                if raison == "occupied":
                    self._erreur_fatale = "Un autre relais est déjà actif sur " + url + " : l'arrêter avant de reprendre"
                    journal.log("alerte", "connexion", self._erreur_fatale)
                    session, attente = None, self.ATTENTE_OCCUPE
                elif raison == "non_enrole":
                    self._erreur_fatale = str(exc)
                    attente = 2
                elif raison == "code_refuse":
                    self._erreur_fatale = str(exc)
                    journal.log("erreur", "connexion", message)
                    c.oublier_session()
                    c.ecart_s = None  # remesurer l'horloge avant le prochain essai
                    session, attente = None, self.ATTENTE_CODE_REFUSE
                    echecs += 1
                elif raison == "bloque":
                    self._erreur_fatale = str(exc)
                    journal.log("erreur", "connexion", message)
                    session, attente = None, self.ATTENTE_BLOQUE
                else:
                    self._erreur_fatale = ""
                    journal.log("alerte", "connexion", message)
                    if raison in ("horloge",):
                        c.ecart_s = None
                    session = None
                    echecs += 1
                if echecs >= self.ECHECS_BASCULE and len(cibles) > 1:
                    with self._verrou:
                        self._idx = (idx + 1) % len(cibles)
                        suivante = cibles[self._idx]
                        self._bascule_t = time.monotonic()
                    self._stats["bascules"] += 1
                    journal.log("alerte", "redondance", str(echecs) + " échecs sur " + url + " : bascule vers " + suivante)
                    session, echecs, delai, attente = None, 0, 1.0, 0.2
                self._maj_phase()
                if attente is None:
                    attente = delai * random.uniform(.85, 1.15)
                    delai = min(self.DELAI_MAX, delai * 2)
                if arret.wait(attente):
                    break
        if c is not None and session:
            self._liberer(c, session, sequence)
        with self._verrou:
            if gen == self._generation:
                self._connectee = False

    # ------------------------------------------------------------ état
    def _maj_phase(self) -> None:
        with self._verrou:
            if not self.actif:
                phase, message = "arret", "Relais à l'arrêt"
            elif self._erreur_fatale:
                phase, message = "erreur", self._erreur_fatale
            elif self._pp is None:
                phase, message = "recherche_pp", self._erreur_pp or "Recherche de ProPresenter"
            elif not self._connectee:
                phase, message = "connexion", self._erreur_cible or "Connexion au serveur"
            else:
                phase = "en_ligne"
                if self.mode == "manuel":
                    message = "Connecté, mode Manuel : aucun texte envoyé"
                elif self._dernier_texte:
                    message = "Connecté, texte à l'antenne"
                else:
                    message = "Connecté, en attente d'un texte"
            if phase != self._phase:
                self._phase, self._phase_depuis = phase, time.time()
            self._message = message

    def oublier_sessions(self) -> None:
        """Après un enrôlement ou un oubli : les cookies ouverts avec l'ancien secret sont abandonnés."""
        with self._verrou:
            clients = list(self._clients.values())
        for c in clients:
            c.oublier_session()
        self._reveil.set()

    def url_cible(self) -> str:
        cibles = self._cibles()
        return cibles[min(self._idx, len(cibles) - 1)]

    def port_pp(self) -> int | None:
        with self._verrou:
            return self._pp["port"] if self._pp else None

    def etat(self) -> dict:
        self._maj_phase()
        self._horloge_en_fond()
        with self._verrou:
            ecart = self._ecart
            return {
                "relais": {
                    "actif": self.actif,
                    "phase": self._phase,
                    "message": self._message,
                    "pp": dict(self._pp) if self._pp else None,
                    "cible": {"url": self.url_cible(), "connectee": bool(self.actif and self._connectee)},
                    "dernier_texte": self._dernier_texte if self.actif else "",
                    "depuis": self._depuis,
                    "dernier_envoi": self._dernier_envoi,
                    "phase_depuis": self._phase_depuis,
                    "transport": self._transport,
                },
                "mode": self.mode,
                "enrole": bool(self._secret()),
                "horloge": {"ecart_s": None if ecart is None else round(ecart, 2),
                            "ok": ecart is None or abs(ecart) <= self.TOLERANCE_HORLOGE_S},
            }

    def diagnostic(self) -> dict:
        """Teste chaque maillon : ProPresenter, Internet, serveur, session, horloge."""
        etapes = []

        def noter(nom, ok, detail):
            etapes.append({"nom": nom, "ok": bool(ok), "detail": journal.masquer(detail)})
            journal.log("info" if ok else "alerte", "diagnostic", nom + " : " + detail)

        try:
            port, description = pp.decouvrir(int(self._reglages().get("port_pp") or 0), self._dernier_port)
            couches = pp.requete(port, "/v1/status/layers", delai=3)
            actives = [k for k, v in couches.items() if v is True] if isinstance(couches, dict) else []
            noter("ProPresenter", True, description + " sur le port " + str(port) + (", couches actives : " + ", ".join(actives) if actives else ", aucune couche active"))
        except ErreurReseau as exc:
            present = pp.processus_present()
            noter("ProPresenter", False, str(exc) + (" (processus ProPresenter lancé : activer Réseau dans ses préférences)" if present else ""))
        url = self.url_cible()
        hote = urllib.parse.urlsplit(url)
        port_hote = hote.port or (443 if hote.scheme == "https" else 80)
        try:
            socket.getaddrinfo(hote.hostname, port_hote)
            with socket.create_connection((hote.hostname, port_hote), timeout=4):
                pass
            noter("Internet", True, "Connexion TCP vers " + str(hote.hostname) + ":" + str(port_hote) + " établie")
        except OSError as exc:
            noter("Internet", False, "Pas de connexion vers " + str(hote.hostname) + " (" + type(exc).__name__ + ") : vérifier le réseau du PC")
        c = self._client(url)
        try:
            s = c.session_publique()
            noter("serveur", True, "GET /api/session : régie reconnue sur " + url + " (accès " + ("code à 6 chiffres" if s.get("mode") == "totp" else str(s.get("mode", "?"))) + ")")
        except ErreurCible as exc:
            noter("serveur", False, "GET /api/session sur " + url + " : " + str(exc))
        if not self._secret():
            noter("session", False, "Poste non enrôlé : coller le code d'enrôlement")
        else:
            try:
                if not c.connecte():
                    c.login()
                noter("session", True, "POST /api/login : session ouverte par le code du moment")
            except ErreurCible as exc:
                noter("session", False, "POST /api/login : " + str(exc))
        try:
            ecart = c.heure()
            self._noter_horloge(ecart)
            ok = abs(ecart) <= self.TOLERANCE_HORLOGE_S
            noter("horloge", ok, "Écart avec le serveur : %+.1f s" % ecart + ("" if ok else " (plus de 20 s : régler l'heure de Windows)"))
        except ErreurCible as exc:
            noter("horloge", False, "GET /api/heure : " + str(exc))
        return {"etapes": etapes}

    def statistiques(self) -> dict:
        with self._verrou:
            return dict(self._stats, cible_index=self._idx, source_serveur=self._source_serveur)
