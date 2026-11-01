"""API locale de AME Tech et service de la fenêtre (www/).

Écoute 127.0.0.1 seulement (port 48690, repli 48691-48699). Chaque POST porte
l'en-tête X-Jeton, injecté dans index.html à la place de {{JETON}}. Les
requêtes dont l'en-tête Host n'est pas 127.0.0.1 ou localhost sont refusées
(parade au DNS rebinding), comme celles qu'un site tiers enverrait.
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from ametech import coffre, journal, propresenter as pp, totp
from ametech.version import VERSION

try:
    from ametech import maj
except Exception as _exc:  # module livré par l'agent paquet, absent ou cassé
    maj = None
try:
    from ametech import assistance
except Exception:
    assistance = None
try:
    from ametech import demarrage
except Exception:
    demarrage = None

PORT_BASE = 48690
PORTS = range(PORT_BASE, PORT_BASE + 10)
WWW = Path(__file__).resolve().parent / "www"
MARQUEUR_JETON = "{{JETON}}"
CORPS_MAX = 256 * 1024
TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain; charset=utf-8",
}
PAGE_SECOURS = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>AME Tech</title>
<script>window.AMETECH_JETON="{{JETON}}";</script></head>
<body style="font:16px system-ui;background:#0b1630;color:#fff;padding:30px">
<h1>AME Tech</h1><p>L'interface n'est pas installée (dossier www absent). Le relais et l'API locale fonctionnent :
<a style="color:#f6c56b" href="/api/etat">/api/etat</a>.</p></body></html>"""


def etat_maj() -> dict:
    if maj is not None:
        try:
            return maj.etat()
        except Exception as exc:
            return {"version_locale": VERSION, "version_dispo": None, "statut": "erreur", "message": "Mise à jour : " + type(exc).__name__, "verifie_le": None}
    return {"version_locale": VERSION, "version_dispo": None, "statut": "inconnu", "message": "Module de mise à jour absent", "verifie_le": None}


def etat_assistance() -> dict:
    if assistance is not None:
        try:
            return assistance.etat()
        except Exception:
            pass
    return {"rustdesk": "absent", "id": None, "serveur": "tech.ame.paris"}


# ---------------------------------------------------------------- instance unique et fenêtre

def instance_active() -> str | None:
    """URL d'une instance AME Tech déjà lancée sur ce poste, sinon None."""
    for port in PORTS:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.6)
            conn.request("GET", "/api/instance", headers={"Host": "127.0.0.1:" + str(port)})
            r = conn.getresponse()
            corps = r.read(4096)
            conn.close()
            if r.status == 200 and json.loads(corps).get("application") == "AME Tech":
                return "http://127.0.0.1:" + str(port) + "/"
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            continue
    return None


def _edge() -> str | None:
    if os.name != "nt":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if exe.exists():
                return str(exe)
    return shutil.which("msedge")


def ouvrir_fenetre(url: str) -> str:
    """Fenêtre Edge en mode application, sinon navigateur par défaut. Renvoie le moyen utilisé."""
    if os.environ.get("AMETECH_SANS_FENETRE"):
        return "aucune"
    exe = _edge()
    if exe:
        profil = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AME Tech" / "edge"
        try:
            subprocess.Popen([exe, "--app=" + url, "--window-size=980,760", "--user-data-dir=" + str(profil),
                              "--no-first-run", "--no-default-browser-check"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            return "edge"
        except OSError as exc:
            journal.log("alerte", "fenetre", "Edge n'a pas démarré (" + type(exc).__name__ + ") : navigateur par défaut")
    try:
        webbrowser.open(url)
        return "navigateur"
    except Exception:
        journal.log("erreur", "fenetre", "Aucun navigateur n'a pu ouvrir " + url)
        return "aucune"


# ---------------------------------------------------------------- serveur

class _Serveur(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    # Sous Windows, SO_REUSEADDR laisserait une seconde instance écouter le même port.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ServeurUI:
    def __init__(self, relais, *, www: Path | None = None):
        self.relais = relais
        self.www = Path(www) if www else WWW
        self.jeton = secrets.token_urlsafe(32)
        self.serveur: _Serveur | None = None
        self.port: int | None = None
        self.dernier_diagnostic: dict | None = None
        journal.fournisseur_diagnostic = self._diagnostic_rapport

    def _diagnostic_rapport(self) -> dict:
        return {"etat": self.relais.etat(), "statistiques": self.relais.statistiques(), "dernier_diagnostic": self.dernier_diagnostic}

    @property
    def url(self) -> str:
        return "http://127.0.0.1:" + str(self.port) + "/"

    def demarrer(self, port: int | None = None) -> int:
        ports = [port] if port is not None else list(PORTS)
        derniere = None
        for p in ports:
            try:
                self.serveur = _Serveur(("127.0.0.1", p), _fabrique(self))
                self.port = self.serveur.server_address[1]
                journal.log("info", "interface", "API locale sur " + self.url)
                return self.port
            except OSError as exc:
                derniere = exc
        raise OSError("Aucun port local libre entre 48690 et 48699 : " + str(derniere))

    def servir(self) -> None:
        try:
            self.serveur.serve_forever(poll_interval=0.5)
        finally:
            self.serveur.server_close()

    def servir_en_fond(self) -> threading.Thread:
        t = threading.Thread(target=self.servir, name="ametech-ui", daemon=True)
        t.start()
        return t

    def arreter(self) -> None:
        if self.serveur is not None:
            self.serveur.shutdown()


def _hote_autorise(entete: str, port: int) -> bool:
    return entete in {"127.0.0.1:" + str(port), "localhost:" + str(port)}


def _chemin_pp_valide(chemin) -> bool:
    return (isinstance(chemin, str) and chemin.startswith("/v1/") and len(chemin) <= 500
            and ".." not in chemin and "//" not in chemin and "\\" not in chemin and "@" not in chemin
            and "#" not in chemin and all(32 < ord(c) < 127 for c in chemin))


def _fabrique(ui: ServeurUI):
    relais = ui.relais

    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "AMETech/" + VERSION
        sys_version = ""

        def log_message(self, *args):
            pass

        # ------------------------------------------------ réponses
        def _entetes_communs(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")

        def json(self, code: int, objet) -> None:
            brut = json.dumps(objet, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(brut)))
            self._entetes_communs()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(brut)

        def octets(self, code: int, brut: bytes, type_mime: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Length", str(len(brut)))
            self._entetes_communs()
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(brut)

        # ------------------------------------------------ garde-fous
        def _garde(self) -> bool:
            if not _hote_autorise(self.headers.get("Host", ""), ui.port):
                self.json(403, {"erreur": "Hôte refusé"})
                return False
            if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
                self.json(403, {"erreur": "Requête d'un autre site refusée"})
                return False
            origine = self.headers.get("Origin")
            if origine is not None and origine not in {"http://127.0.0.1:" + str(ui.port), "http://localhost:" + str(ui.port)}:
                self.json(403, {"erreur": "Origine refusée"})
                return False
            return True

        def _jeton_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Jeton", ""), ui.jeton)

        def _corps(self):
            taille = int(self.headers.get("Content-Length") or 0)
            if not 0 <= taille <= CORPS_MAX:
                raise ValueError("Requête trop longue")
            brut = self.rfile.read(taille) if taille else b""
            if not brut.strip():
                return {}
            return json.loads(brut)

        # ------------------------------------------------ GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._garde():
                return
            url = urlsplit(self.path)
            chemin = url.path
            try:
                if chemin == "/api/instance":
                    return self.json(200, {"application": "AME Tech", "version": VERSION})
                if chemin == "/api/etat":
                    return self.json(200, {"version": VERSION, **relais.etat(), "maj": etat_maj(), "assistance": etat_assistance()})
                if chemin == "/api/code":
                    try:
                        return self.json(200, relais.code())
                    except PermissionError:
                        return self.json(403, {"erreur": "Poste non enrôlé"})
                if chemin == "/api/journal":
                    try:
                        n = int(parse_qs(url.query).get("depuis", ["0"])[0])
                    except ValueError:
                        n = 0
                    lignes, dernier = journal.depuis(n)
                    return self.json(200, {"lignes": [{k: l.get(k) for k in ("n", "t", "niveau", "etape", "message")} for l in lignes[-500:]], "dernier": dernier})
                if chemin == "/api/diagnostic":
                    ui.dernier_diagnostic = relais.diagnostic()
                    return self.json(200, ui.dernier_diagnostic)
                if chemin == "/api/rapport":
                    nom = "AME-Tech-rapport-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
                    return self.octets(200, journal.rapport_zip(), "application/zip", {"Content-Disposition": 'attachment; filename="' + nom + '"'})
                if chemin == "/api/pp/info":
                    return self._pp_info()
                if chemin == "/api/pp/flux":
                    return self._pp_flux()
                if chemin == "/api/reglages":
                    return self.json(200, self._reglages_publics())
                if chemin.startswith("/api/"):
                    return self.json(404, {"erreur": "Route inconnue"})
                return self._statique(chemin)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                journal.log("erreur", "interface", "GET " + chemin + " : " + type(exc).__name__ + " " + str(exc)[:200])
                try:
                    self.json(500, {"erreur": "Erreur interne : " + type(exc).__name__})
                except OSError:
                    pass

        # ------------------------------------------------ POST
        def do_POST(self):
            if not self._garde():
                return
            chemin = urlsplit(self.path).path
            if not self._jeton_ok():
                return self.json(403, {"erreur": "Jeton local absent ou invalide"})
            try:
                corps = self._corps()
                if not isinstance(corps, (dict, list)) and corps is not None:
                    raise ValueError("Objet JSON attendu")
                corps = corps if isinstance(corps, dict) else {}
                if chemin == "/api/enroler":
                    secret = totp.lire_enrolement(str(corps.get("texte", "")))
                    journal.enregistrer_sensible(secret)
                    coffre.ecrire({"secret_totp": secret})
                    relais.oublier_sessions() if hasattr(relais, "oublier_sessions") else None
                    journal.log("info", "enrolement", "Poste enrôlé : le code à 6 chiffres est disponible")
                    return self.json(200, {"ok": True})
                if chemin == "/api/oublier":
                    coffre.ecrire({"secret_totp": ""})
                    relais.oublier_sessions() if hasattr(relais, "oublier_sessions") else None
                    journal.log("info", "enrolement", "Secret effacé : le poste doit être réenrôlé")
                    return self.json(200, {"ok": True})
                if chemin == "/api/relais/demarrer":
                    relais.demarrer()
                    return self.json(200, {"ok": True})
                if chemin == "/api/relais/arreter":
                    relais.arreter()
                    return self.json(200, {"ok": True})
                if chemin == "/api/mode":
                    relais.definir_mode(str(corps.get("mode", "")))
                    return self.json(200, {"ok": True})
                if chemin == "/api/reglages":
                    return self.json(200, self._ecrire_reglages(corps))
                if chemin == "/api/pp/requete":
                    return self._pp_requete(corps)
                if chemin in ("/api/maj/verifier", "/api/maj/appliquer"):
                    if maj is None:
                        return self.json(503, {**etat_maj(), "erreur": "Module de mise à jour absent"})
                    resultat = maj.verifier() if chemin.endswith("verifier") else maj.appliquer()
                    return self.json(200, resultat)
                if chemin == "/api/assistance/rustdesk":
                    if assistance is None:
                        return self.json(503, {**etat_assistance(), "erreur": "Module d'assistance absent"})
                    return self.json(200, assistance.lancer_rustdesk())
                if chemin == "/api/quitter":
                    self.json(200, {"ok": True})
                    journal.log("info", "interface", "Fermeture demandée depuis la fenêtre")

                    def quitter():
                        relais.arreter()
                        ui.arreter()
                    threading.Thread(target=quitter, daemon=True).start()
                    return
                return self.json(404, {"erreur": "Route inconnue"})
            except (ValueError, TypeError) as exc:
                return self.json(400, {"erreur": str(exc) or "Requête invalide"})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                journal.log("erreur", "interface", "POST " + chemin + " : " + type(exc).__name__ + " " + str(exc)[:200])
                return self.json(500, {"erreur": "Erreur interne : " + type(exc).__name__})

        # ------------------------------------------------ réglages
        def _reglages_publics(self) -> dict:
            r = coffre.lire()
            demarrage_actif = r["demarrage_windows"]
            if demarrage is not None:
                try:
                    demarrage_actif = bool(demarrage.actif())
                except Exception:
                    pass
            return {"cibles": r["cibles"], "port_pp": r["port_pp"], "demarrage_windows": demarrage_actif,
                    "relais_au_lancement": r["relais_au_lancement"]}

        def _ecrire_reglages(self, corps: dict) -> dict:
            from ametech import cible
            champs = {}
            if "cibles" in corps:
                if not isinstance(corps["cibles"], list) or not 1 <= len(corps["cibles"]) <= 5:
                    raise ValueError("Indiquer de 1 à 5 adresses de serveur")
                champs["cibles"] = [cible.valider_url(u) for u in corps["cibles"]]
            if "port_pp" in corps:
                port = corps["port_pp"]
                if isinstance(port, str) and port.strip().isdigit():
                    port = int(port)
                if port in ("", None):
                    port = 0
                if type(port) is not int or not 0 <= port <= 65535:
                    raise ValueError("Port ProPresenter invalide (0 = automatique)")
                champs["port_pp"] = port
            for cle in ("demarrage_windows", "relais_au_lancement"):
                if cle in corps:
                    if not isinstance(corps[cle], bool):
                        raise ValueError("Valeur attendue : vrai ou faux pour " + cle)
                    champs[cle] = corps[cle]
            if "demarrage_windows" in champs and demarrage is not None:
                try:
                    demarrage.activer(champs["demarrage_windows"])
                except Exception as exc:
                    raise ValueError("Démarrage avec Windows non modifié : " + type(exc).__name__)
            if champs:
                coffre.ecrire(champs)
                journal.log("info", "reglages", "Réglages modifiés : " + ", ".join(sorted(champs)))
            return self._reglages_publics()

        # ------------------------------------------------ ProPresenter
        def _port_pp(self) -> int:
            port = relais.port_pp()
            if port:
                return port
            port, _ = pp.decouvrir(int(coffre.lire().get("port_pp") or 0))
            return port

        def _pp_info(self):
            try:
                port = self._port_pp()
                version = pp.requete(port, "/version", delai=3)
                couches = pp.requete(port, "/v1/status/layers", delai=3)
            except pp.ErreurReseau as exc:
                return self.json(503, {"erreur": str(exc), "trouve": False})
            version = version if isinstance(version, dict) else {}
            couches = couches if isinstance(couches, dict) else {}
            return self.json(200, {"trouve": True, "port": port, "nom": version.get("host_description", ""),
                                   "version": pp.version_depuis(str(version.get("host_description", ""))),
                                   "api_version": version.get("api_version", ""), "plateforme": version.get("platform", ""),
                                   "couches": couches, "actives": [k for k, v in couches.items() if v is True]})

        def _pp_requete(self, corps: dict):
            methode = corps.get("methode", "GET")
            chemin = corps.get("chemin")
            if methode not in ("GET", "POST", "PUT"):
                raise ValueError("Méthode autorisée : GET, POST ou PUT")
            if not _chemin_pp_valide(chemin):
                raise ValueError("Chemin limité à /v1/...")
            try:
                port = self._port_pp()
                statut, brut = pp.requete(port, chemin, None if methode == "GET" else corps.get("corps"), methode=methode, brut=True, delai=6)
            except pp.ErreurReseau as exc:
                return self.json(502, {"erreur": str(exc)})
            journal.log("info", "molette", methode + " " + chemin + " : HTTP " + str(statut))
            try:
                valeur = json.loads(brut) if brut.strip() else None
            except ValueError:
                valeur = brut[:65536].decode("utf-8", "replace")
            return self.json(200, {"statut": statut, "corps": valeur})

        def _pp_flux(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self._entetes_communs()
            self.end_headers()
            self.close_connection = True

            def envoyer(texte: str):
                self.wfile.write(texte.encode())
                self.wfile.flush()
            envoyer(": AME Tech\n\n")
            while True:
                reponse = None
                try:
                    port = self._port_pp()
                    reponse = pp.ouvrir_flux(port)
                    envoyer("event: etat\ndata: " + json.dumps({"connecte": True, "port": port}) + "\n\n")
                    for env in pp.objets_flux(pp.morceaux(reponse)):
                        envoyer("data: " + json.dumps(env, ensure_ascii=False) + "\n\n")
                except pp.ErreurReseau as exc:
                    envoyer("event: erreur\ndata: " + json.dumps({"erreur": str(exc)}, ensure_ascii=False) + "\n\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    if reponse is not None:
                        try:
                            reponse.close()
                        except OSError:
                            pass
                time.sleep(3)

        # ------------------------------------------------ fichiers statiques
        def _statique(self, chemin: str):
            decode = unquote(chemin)
            if "\x00" in decode or "\\" in decode or any(p in {".", ".."} for p in decode.split("/")):
                return self.json(403, {"erreur": "Chemin refusé"})
            if decode in ("/", "/index.html"):
                return self._index()
            base = ui.www.resolve()
            cible = (base / decode.lstrip("/")).resolve()
            if base not in cible.parents or not cible.is_file():
                return self.json(404, {"erreur": "Fichier absent"})
            type_mime = TYPES.get(cible.suffix.lower(), "application/octet-stream")
            return self.octets(200, cible.read_bytes(), type_mime)

        def _index(self):
            try:
                html = (ui.www / "index.html").read_text(encoding="utf-8")
            except OSError:
                html = PAGE_SECOURS
            if MARQUEUR_JETON in html:
                html = html.replace(MARQUEUR_JETON, ui.jeton)
            else:
                balise = '<script>window.AMETECH_JETON="' + ui.jeton + '";</script>'
                html = html.replace("</head>", balise + "</head>", 1) if "</head>" in html else balise + html
            empreintes = []
            for m in re.finditer(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S | re.I):
                if "src=" not in (m.group(1) or "") and "AMETECH_JETON" in m.group(2):
                    empreintes.append("'sha256-" + base64.b64encode(hashlib.sha256(m.group(2).encode()).digest()).decode() + "'")
            csp = ("default-src 'self'; script-src 'self' " + " ".join(empreintes) + "; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                   "frame-ancestors 'none'; form-action 'self'")
            return self.octets(200, html.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    return Gestionnaire
