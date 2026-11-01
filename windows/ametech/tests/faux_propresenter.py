"""Faux ProPresenter (API v1) pour les tests : /version, couches, diapo, flux /v1/status/updates.

Le flux suit le format natif (objets JSON séparés par CRLF CRLF) et envoie
timer/system_time toutes les 0,5 s, comme le vrai logiciel qui tient ainsi la
connexion ouverte.
"""
from __future__ import annotations
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FauxProPresenter:
    def __init__(self, description: str = "ProPresenter 7.16.2"):
        self.description = description
        self.condition = threading.Condition()
        self.rev = 0
        self.slide_actif = False
        self.texte = ""
        self.notes = ""
        self.flux_actif = True
        self.requetes: list[tuple[str, str]] = []
        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), self._fabrique())
        self.serveur.daemon_threads = True
        self.port = self.serveur.server_address[1]
        self._fil = threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._fil.start()
        self._ferme = False

    def changer(self, texte: str, notes: str = "", actif: bool = True) -> None:
        with self.condition:
            self.texte, self.notes, self.slide_actif = texte, notes, actif
            self.rev += 1
            self.condition.notify_all()

    def eteindre(self) -> None:
        with self.condition:
            self.slide_actif = False
            self.rev += 1
            self.condition.notify_all()

    def fermer(self) -> None:
        self._ferme = True
        with self.condition:
            self.condition.notify_all()
        self.serveur.shutdown()
        self.serveur.server_close()

    def couches(self) -> dict:
        return {"video_input": False, "slide": self.slide_actif, "media": False, "props": False,
                "messages": False, "audio": False, "announcements": False}

    def diapo(self) -> dict:
        return {"current": {"text": self.texte, "notes": self.notes, "uuid": "faux-" + str(self.rev)},
                "next": {"text": "DIAPO SUIVANTE A NE JAMAIS ENVOYER", "notes": "", "uuid": "suivante"}}

    def _fabrique(self):
        faux = self

        class Gestionnaire(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _json(self, code, objet):
                brut = json.dumps(objet, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(brut)))
                self.end_headers()
                self.wfile.write(brut)

            def do_GET(self):
                faux.requetes.append(("GET", self.path))
                if self.path == "/version":
                    return self._json(200, {"api_version": "v1", "host_description": faux.description, "platform": "win", "os_version": "10.0"})
                if self.path == "/v1/status/layers":
                    return self._json(200, faux.couches())
                if self.path == "/v1/status/slide":
                    return self._json(200, faux.diapo())
                if self.path.startswith("/v1/"):
                    return self._json(200, {"chemin": self.path})
                return self._json(404, {"erreur": "inconnu"})

            def do_PUT(self):
                faux.requetes.append(("PUT", self.path))
                taille = int(self.headers.get("Content-Length") or 0)
                corps = self.rfile.read(taille) if taille else b""
                return self._json(200, {"recu": json.loads(corps) if corps else None})

            def do_POST(self):
                faux.requetes.append(("POST", self.path))
                taille = int(self.headers.get("Content-Length") or 0)
                corps = self.rfile.read(taille) if taille else b""
                if self.path != "/v1/status/updates":
                    return self._json(200, {"recu": json.loads(corps) if corps else None})
                if not faux.flux_actif:
                    return self._json(404, {"erreur": "flux absent"})
                chemins = json.loads(corps or b"[]")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()

                def envoyer(chemin, donnees):
                    self.wfile.write((json.dumps({"url": chemin, "data": donnees}, ensure_ascii=False) + "\r\n\r\n").encode())
                    self.wfile.flush()
                try:
                    with faux.condition:
                        rev = faux.rev
                        couches, diapo = faux.couches(), faux.diapo()
                    if "status/slide" in chemins:
                        envoyer("status/slide", diapo)
                    if "status/layers" in chemins:
                        envoyer("status/layers", couches)
                    while not faux._ferme:
                        with faux.condition:
                            faux.condition.wait(0.5)
                            change = faux.rev != rev
                            rev = faux.rev
                            couches, diapo = faux.couches(), faux.diapo()
                        if change:
                            envoyer("status/slide", diapo)
                            envoyer("status/layers", couches)
                        envoyer("timer/system_time", int(time.time()))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        return Gestionnaire
