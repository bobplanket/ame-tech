#!/usr/bin/env python3
"""Maquette de l'API locale d'AME Tech, pour développer et tester l'interface (www/).

Stdlib uniquement. Sert windows/ametech/www/ et simule toutes les routes de ui_server.py
décrites dans docs/CONTRAT.md, avec un jeton anti-CSRF injecté dans index.html à la place
du marqueur {{JETON}} et vérifié sur chaque POST (en-tête X-Jeton, 403 sinon).

Lancement :
    python3 windows/outils/maquette_api.py [--port 48680] [--scenario normal]

Scénarios (--scenario, ou GET /_maquette/scenario?nom=... pendant l'exécution) :
    normal       relais arrêté ; Démarrer enchaîne recherche, connexion puis en ligne
    arret        relais arrêté, figé
    recherche    recherche de ProPresenter, figé
    connexion    connexion au serveur, figé
    en_ligne     en ligne, figé, avec un dernier texte
    erreur       erreur avec message
    non_enrole   poste non enrôlé (403 sur /api/code)
    horloge      en ligne mais horloge décalée de 94 s
    maj          mise à jour 3.0.1 disponible
    rustdesk     RustDesk lancé avec un identifiant
    assistance_ko  le bouton Assistance à distance renvoie ok=false (comme hors Windows)
    panne        /api/* répond 503 (simule un ui_server planté)

Routes de pilotage (maquette seulement) :
    GET /_maquette/scenario?nom=X   change de scénario et réinitialise l'état
    GET /_maquette/requetes         derniers POST reçus (chemin, corps, jeton présent et valide)
    GET /_maquette/jeton            jeton de la session (pour les tests automatisés)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WWW = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ametech", "www"))
MARQUEUR = "{{JETON}}"
VERSION = "3.0.0"
SCENARIOS = ("normal", "arret", "recherche", "connexion", "en_ligne", "erreur", "non_enrole",
             "horloge", "maj", "rustdesk", "assistance_ko", "panne")

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
         ".woff2": "font/woff2", ".ico": "image/x-icon"}

TEXTES = ["Grâce infinie, ô quel doux son", "Jean 3:16 Car Dieu a tant aimé le monde",
          "Tu es digne de louange", "Psaume 23:1 L'Éternel est mon berger"]


class Etat:
    def __init__(self, scenario: str):
        self.verrou = threading.Lock()
        self.journal: list[dict] = []
        self.n = 0
        self.requetes: list[dict] = []
        self.reinitialiser(scenario)

    def reinitialiser(self, scenario: str) -> None:
        with self.verrou:
            self.scenario = scenario
            self.enrole = scenario != "non_enrole"
            self.mode = "auto"
            self.actif = scenario in ("recherche", "connexion", "en_ligne", "erreur", "horloge", "maj", "rustdesk")
            self.demarre_le = time.time() - 754 if self.actif else None
            self.changement = (time.time(), "arret")
            self.reglages = {"cibles": ["https://tech.ame.paris"], "port_pp": 0,
                             "demarrage_windows": False, "relais_au_lancement": True}
            self.maj = {"version_locale": VERSION, "version_dispo": None, "statut": "a_jour",
                        "message": "", "verifie_le": time.time() - 3600}
            if scenario == "maj":
                self.maj.update(version_dispo="3.0.1", statut="disponible",
                                message="Correctif de reconnexion au serveur")
            self.assistance = {"rustdesk": "absent", "id": None, "serveur": "tech.ame.paris"}
            if scenario == "rustdesk":
                self.assistance = {"rustdesk": "lance", "id": "1482093557", "serveur": "tech.ame.paris"}
        self.log("info", "maquette", f"Scénario « {scenario} » chargé")

    def log(self, niveau: str, etape: str, message: str) -> None:
        with self.verrou:
            self.n += 1
            self.journal.append({"n": self.n, "t": time.time(), "niveau": niveau, "etape": etape, "message": message})
            del self.journal[:-2000]

    def phase(self) -> str:
        s = self.scenario
        if not self.enrole or not self.actif:
            return "arret"
        if s in ("recherche",):
            return "recherche_pp"
        if s == "connexion":
            return "connexion"
        if s == "erreur":
            return "erreur"
        if s == "normal":
            ecoule = time.time() - (self.demarre_le or time.time())
            return "recherche_pp" if ecoule < 2 else "connexion" if ecoule < 4 else "en_ligne"
        return "en_ligne"

    def relais(self) -> dict:
        ph = self.phase()
        messages = {"arret": "", "recherche_pp": "Recherche de ProPresenter sur les ports 1025 à 65535",
                    "connexion": "Ouverture de session sur tech.ame.paris",
                    "en_ligne": "Textes transmis à tech.ame.paris",
                    "erreur": "Serveur injoignable : délai dépassé après 3 essais (tech.ame.paris)"}
        pp = None
        if ph in ("connexion", "en_ligne") or (ph == "erreur"):
            pp = {"port": 50001, "version": "18.4.2", "nom": "ProPresenter"}
        if self.changement[1] != ph:
            self.changement = (time.time() if self.scenario == "normal" else (self.demarre_le or time.time()), ph)
        t7 = int(time.time() // 7)
        return {"actif": self.actif and self.enrole, "phase": ph, "message": messages[ph], "pp": pp,
                "cible": {"url": self.reglages["cibles"][0], "connectee": ph == "en_ligne"},
                "dernier_texte": TEXTES[t7 % len(TEXTES)] if ph == "en_ligne" else "",
                "depuis": t7 * 7 if ph == "en_ligne" else None,
                # Champs en plus du contrat, renvoyés par le vrai ui_server.py :
                "phase_depuis": self.changement[0], "transport": "Flux natif" if ph == "en_ligne" else "",
                "dernier_envoi": t7 * 7 if ph == "en_ligne" else None}

    def etat(self) -> dict:
        horloge = {"ecart_s": 0.4, "ok": True}
        if self.scenario == "horloge":
            horloge = {"ecart_s": 94.2, "ok": False}
        return {"version": VERSION, "relais": self.relais(), "mode": self.mode, "enrole": self.enrole,
                "horloge": horloge, "maj": dict(self.maj), "assistance": dict(self.assistance)}


def code_du_moment() -> dict:
    t = int(time.time())
    graine = hashlib.sha256(f"maquette-{t // 30}".encode()).hexdigest()
    return {"code": f"{int(graine[:8], 16) % 1_000_000:06d}", "reste_s": 30 - t % 30}


def fabriquer(etat: Etat, jeton: str):
    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "AMETechMaquette/1"

        def log_message(self, fmt, *args):  # silencieux, le journal de la maquette suffit
            pass

        # -- réponses
        def json(self, code: int, donnees) -> None:
            corps = json.dumps(donnees, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def fichier(self, chemin: str) -> None:
            rel = chemin.lstrip("/") or "index.html"
            complet = os.path.normpath(os.path.join(WWW, rel))
            if not complet.startswith(WWW + os.sep) or not os.path.isfile(complet):
                self.send_error(404)
                return
            with open(complet, "rb") as f:
                donnees = f.read()
            csp = None
            if rel == "index.html":
                donnees = donnees.replace(MARQUEUR.encode(), jeton.encode())
                # Même CSP que ui_server.py : hash du script inline qui porte le jeton.
                inline = re.search(rb"<script>(.*?AMETECH_JETON.*?)</script>", donnees, re.S)
                empreinte = base64.b64encode(hashlib.sha256(inline.group(1)).digest()).decode() if inline else ""
                csp = (f"default-src 'self'; script-src 'self' 'sha256-{empreinte}'; style-src 'self' 'unsafe-inline'; "
                       "img-src 'self' data:; font-src 'self' data:; connect-src 'self'")
            ext = os.path.splitext(complet)[1].lower()
            self.send_response(200)
            if csp:
                self.send_header("Content-Security-Policy", csp)
            self.send_header("Content-Type", TYPES.get(ext) or mimetypes.guess_type(complet)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(donnees)))
            self.end_headers()
            self.wfile.write(donnees)

        def corps(self):
            n = int(self.headers.get("Content-Length") or 0)
            brut = self.rfile.read(n) if n else b""
            if not brut:
                return {}
            try:
                return json.loads(brut)
            except ValueError:
                return None

        # -- GET
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            if p.startswith("/_maquette/"):
                return self.pilotage(p, q)
            if p.startswith("/api/") and etat.scenario == "panne":
                return self.json(503, {"erreur": "maquette en panne"})
            if p == "/api/etat":
                return self.json(200, etat.etat())
            if p == "/api/code":
                if not etat.enrole:
                    return self.json(403, {"erreur": "poste non enrôlé"})
                return self.json(200, code_du_moment())
            if p == "/api/journal":
                try:
                    depuis = int((q.get("depuis") or ["0"])[0])
                except ValueError:
                    depuis = 0
                with etat.verrou:
                    lignes = [l for l in etat.journal if l["n"] > depuis]
                    dernier = etat.n
                return self.json(200, {"lignes": lignes, "dernier": dernier})
            if p == "/api/diagnostic":
                time.sleep(0.4)
                ph = etat.phase()
                pp_ok = ph in ("connexion", "en_ligne", "erreur")
                etapes = [
                    {"nom": "ProPresenter", "ok": pp_ok, "detail": "ProPresenter 18.4.2 sur le port 50001" if pp_ok else "Aucun processus ProPresenter trouvé"},
                    {"nom": "Internet", "ok": True, "detail": "DNS et HTTPS joignables (42 ms)"},
                    {"nom": "Serveur", "ok": ph != "erreur", "detail": "tech.ame.paris répond (HTTP 200, 88 ms)" if ph != "erreur" else "tech.ame.paris : délai dépassé (10 s)"},
                    {"nom": "Session", "ok": ph == "en_ligne", "detail": "Cookie valide encore 11 h 12" if ph == "en_ligne" else "Pas de session ouverte"},
                    {"nom": "Horloge", "ok": etat.scenario != "horloge", "detail": "Écart de 94,2 s avec le serveur" if etat.scenario == "horloge" else "Écart de 0,4 s"},
                ]
                return self.json(200, {"etapes": etapes})
            if p == "/api/rapport":
                tampon = io.BytesIO()
                with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
                    with etat.verrou:
                        z.writestr("journal.txt", "\n".join(f"{l['n']} {l['niveau']} {l['etape']} {l['message']}" for l in etat.journal))
                    z.writestr("diagnostic.json", json.dumps({"maquette": True}, ensure_ascii=False))
                donnees = tampon.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="ame-tech-rapport.zip"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(donnees)))
                self.end_headers()
                self.wfile.write(donnees)
                return
            if p == "/api/pp/info":
                if etat.phase() not in ("connexion", "en_ligne", "erreur"):
                    return self.json(503, {"erreur": "ProPresenter introuvable sur ce PC", "trouve": False})
                return self.json(200, {"trouve": True, "port": 50001, "nom": "ProPresenter", "version": "18.4.2",
                                       "api_version": "v1", "plateforme": "Windows",
                                       "couches": {"slide": True, "media": False, "audio": False, "props": False},
                                       "actives": ["slide"]})
            if p == "/api/pp/flux":
                return self.flux()
            if p == "/api/reglages":
                return self.json(200, etat.reglages)
            if p.startswith("/api/"):
                return self.json(404, {"erreur": "route inconnue"})
            return self.fichier(p)

        def flux(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            i = 0
            try:
                self.wfile.write(b"retry: 3000\n\nevent: etat\ndata: {\"connecte\": true, \"port\": 50001}\n\n")
                self.wfile.flush()
                while True:
                    i += 1
                    evt = {"url": "status/slide", "data": {"current": {"text": TEXTES[i % len(TEXTES)], "uuid": f"diap-{i}"}}}
                    if i % 3 == 0:
                        evt = {"url": "status/layers", "data": {"slide": True, "media": i % 2 == 0, "audio": False}}
                    self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.8)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def pilotage(self, p, q):
            if p == "/_maquette/scenario":
                nom = (q.get("nom") or [""])[0]
                if nom not in SCENARIOS:
                    return self.json(400, {"erreur": "scénario inconnu", "scenarios": SCENARIOS})
                etat.reinitialiser(nom)
                return self.json(200, {"ok": True, "scenario": nom})
            if p == "/_maquette/requetes":
                with etat.verrou:
                    return self.json(200, {"requetes": list(etat.requetes)})
            if p == "/_maquette/jeton":
                return self.json(200, {"jeton": jeton})
            return self.json(404, {"erreur": "inconnu"})

        # -- POST
        def do_POST(self):
            p = urlparse(self.path).path
            corps = self.corps()
            recu = self.headers.get("X-Jeton")
            valide = bool(recu) and secrets.compare_digest(recu, jeton)
            with etat.verrou:
                etat.requetes.append({"t": time.time(), "chemin": p, "corps": corps, "jeton_present": recu is not None, "jeton_valide": valide})
                del etat.requetes[:-200]
            if not valide:
                return self.json(403, {"erreur": "jeton manquant ou invalide"})
            if etat.scenario == "panne":
                return self.json(503, {"erreur": "maquette en panne"})
            if corps is None:
                return self.json(400, {"erreur": "JSON invalide"})

            if p == "/api/enroler":
                texte = str(corps.get("texte") or "").strip()
                if not (texte.startswith("AMETECH1:") or texte.startswith("otpauth://totp/")):
                    return self.json(400, {"erreur": "code d'enrôlement illisible"})
                if "faux" in texte:
                    return self.json(400, {"erreur": "secret invalide (base32 attendu)"})
                etat.enrole = True
                if etat.scenario == "non_enrole":
                    etat.scenario = "normal"
                etat.log("info", "enrolement", "Poste enrôlé")
                return self.json(200, {"ok": True})
            if p == "/api/oublier":
                etat.enrole, etat.actif = False, False
                etat.log("alerte", "enrolement", "Secret effacé, poste à réenrôler")
                return self.json(200, {"ok": True})
            if p == "/api/relais/demarrer":
                if not etat.enrole:
                    return self.json(400, {"erreur": "poste non enrôlé"})
                if etat.scenario in ("arret",):
                    etat.scenario = "normal"
                etat.actif, etat.demarre_le = True, time.time()
                etat.log("info", "relais", "Démarrage demandé")
                etat.log("info", "propresenter", "Recherche de ProPresenter")
                return self.json(200, {"ok": True})
            if p == "/api/relais/arreter":
                etat.actif, etat.demarre_le = False, None
                etat.log("info", "relais", "Arrêt demandé")
                return self.json(200, {"ok": True})
            if p == "/api/mode":
                mode = corps.get("mode")
                if mode not in ("auto", "manuel"):
                    return self.json(400, {"erreur": "mode inconnu"})
                etat.mode = mode
                etat.log("info", "mode", f"Mode {mode}")
                return self.json(200, {"ok": True})
            if p == "/api/pp/requete":
                chemin = str(corps.get("chemin") or "")
                methode = corps.get("methode")
                if methode not in ("GET", "POST", "PUT") or not chemin.startswith("/v1/"):
                    return self.json(400, {"erreur": "méthode ou chemin refusé (chemin limité à /v1/)"})
                if chemin.startswith("/v1/inconnu"):
                    return self.json(200, {"statut": 404, "corps": "Not Found"})
                return self.json(200, {"statut": 200, "corps": {"methode": methode, "chemin": chemin, "recu": corps.get("corps"),
                                                                 "layers": {"slide": True, "media": False, "audio": False}}})
            if p == "/api/reglages":
                nouveau = dict(etat.reglages)
                for cle in ("cibles", "port_pp", "demarrage_windows", "relais_au_lancement"):
                    if cle in corps:
                        nouveau[cle] = corps[cle]
                if not isinstance(nouveau["cibles"], list) or not nouveau["cibles"]:
                    return self.json(400, {"erreur": "au moins une cible"})
                etat.reglages = nouveau
                etat.log("info", "reglages", "Réglages enregistrés")
                return self.json(200, nouveau)
            if p == "/api/maj/verifier":
                time.sleep(0.6)
                etat.maj["verifie_le"] = time.time()
                if etat.scenario == "maj":
                    etat.maj.update(version_dispo="3.0.1", statut="disponible", message="Correctif de reconnexion au serveur")
                else:
                    etat.maj.update(statut="a_jour", version_dispo=None, message="")
                return self.json(200, dict(etat.maj))
            if p == "/api/maj/appliquer":
                if etat.maj["statut"] not in ("disponible", "pret"):
                    return self.json(400, {"erreur": "aucune mise à jour à installer"})
                etat.maj.update(statut="telechargement", message="Téléchargement de 3.0.1")
                etat.log("info", "maj", "Téléchargement de la version 3.0.1")
                return self.json(200, dict(etat.maj))
            if p == "/api/assistance/rustdesk":
                time.sleep(1.2)
                if etat.scenario == "assistance_ko":
                    return self.json(200, dict(etat.assistance, ok=False,
                                               message="L'assistance RustDesk n'est disponible que sous Windows."))
                etat.assistance = {"rustdesk": "lance", "id": "1482093557", "serveur": "tech.ame.paris"}
                etat.log("info", "assistance", "RustDesk lancé")
                return self.json(200, dict(etat.assistance, ok=True, message="RustDesk est lancé, identifiant 148 209 355 7."))
            if p == "/api/quitter":
                etat.log("info", "application", "Arrêt demandé depuis la fenêtre")
                return self.json(200, {"ok": True})
            return self.json(404, {"erreur": "route inconnue"})

    return Gestionnaire


def animer(etat: Etat) -> None:
    """Fait vivre le journal : textes envoyés, alertes, erreurs selon la phase."""
    i = 0
    while True:
        time.sleep(1.5)
        i += 1
        ph = etat.phase()
        if ph == "en_ligne":
            etat.log("info", "envoi", f"Texte transmis : {TEXTES[i % len(TEXTES)]}")
        elif ph == "recherche_pp":
            etat.log("alerte", "propresenter", "ProPresenter introuvable, nouvel essai dans 2 s")
        elif ph == "connexion":
            etat.log("info", "cible", "Connexion à https://tech.ame.paris")
        elif ph == "erreur":
            etat.log("erreur", "cible", "POST /api/presenter/frame : délai dépassé")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=48680)
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal")
    args = ap.parse_args()
    jeton = secrets.token_urlsafe(24)
    etat = Etat(args.scenario)
    threading.Thread(target=animer, args=(etat,), daemon=True).start()
    serveur = ThreadingHTTPServer(("127.0.0.1", args.port), fabriquer(etat, jeton))
    serveur.daemon_threads = True
    print(f"Maquette AME Tech sur http://127.0.0.1:{args.port}/ (scénario {args.scenario})", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
