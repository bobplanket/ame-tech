"""Tests du relais AME Tech : faux ProPresenter local et vrai server_v2.py en mode totp.

Lancer depuis windows/ :  python3 -m unittest ametech.tests.test_relais -v
Le serveur cible est cherché dans AMETECH_SERVEUR_V2, sinon ~/clawd/badaboum/badaboum-live-v2 ;
les tests de bout en bout sont sautés s'il est absent. Aucun secret fixe : le secret
TOTP et le mot de passe admin de fixture sont tirés au hasard à chaque lancement.
"""
from __future__ import annotations
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

DOSSIER_TEST = tempfile.mkdtemp(prefix="ametech-test-")
os.environ["AMETECH_DOSSIER"] = DOSSIER_TEST
os.environ["AMETECH_SANS_FENETRE"] = "1"

from ametech import coffre, journal, totp, ui_server  # noqa: E402
from ametech import propresenter as pp  # noqa: E402
from ametech.cible import Cible  # noqa: E402
from ametech.relais import Relais  # noqa: E402
from ametech.tests.faux_propresenter import FauxProPresenter  # noqa: E402

SERVEUR_V2 = Path(os.environ.get("AMETECH_SERVEUR_V2") or Path.home() / "clawd" / "badaboum" / "badaboum-live-v2")
SERVEUR_PRESENT = (SERVEUR_V2 / "server_v2.py").exists()


def tearDownModule():
    shutil.rmtree(DOSSIER_TEST, ignore_errors=True)


def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def attendre(predicat, delai=10.0, pas=0.1, message="condition non atteinte"):
    fin = time.monotonic() + delai
    derniere = None
    while time.monotonic() < fin:
        try:
            derniere = predicat()
            if derniere:
                return derniere
        except Exception as exc:  # le serveur peut être en train de redémarrer
            derniere = exc
        time.sleep(pas)
    raise AssertionError(message + " (dernier résultat : " + repr(derniere)[:300] + ")")


def http_json(port, methode, chemin, corps=None, entetes=None, delai=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=delai)
    h = dict(entetes or {})
    brut = None
    if corps is not None:
        brut = json.dumps(corps).encode()
        h["Content-Type"] = "application/json"
    conn.request(methode, chemin, body=brut, headers=h)
    r = conn.getresponse()
    donnees = r.read()
    en_tetes = dict(r.getheaders())
    conn.close()
    try:
        valeur = json.loads(donnees) if donnees else None
    except ValueError:
        valeur = donnees
    return r.status, valeur, en_tetes


def reinitialiser_coffre(**champs):
    try:
        coffre.chemin().unlink()
    except FileNotFoundError:
        pass
    if champs:
        coffre.ecrire(champs)


# ---------------------------------------------------------------- serveur cible réel

class ServeurCible:
    """server_v2.py lancé en mode totp dans un dossier temporaire."""

    def __init__(self, secret: str, empreinte: str):
        self.port = port_libre()
        self.data = Path(tempfile.mkdtemp(prefix="ametech-cible-"))
        (self.data / "totp.json").write_text(json.dumps({"secret": secret, "version": 1, "cree": time.time()}))
        self.secret, self.empreinte = secret, empreinte
        self.proc = None
        self.url = "http://127.0.0.1:" + str(self.port)

    def lancer(self):
        env = {**os.environ, "AME_ADMIN_HASH": self.empreinte}
        env.pop("BADABOUM_PIN", None)
        self.sortie = open(self.data / "serveur.log", "ab")
        self.proc = subprocess.Popen([sys.executable, str(SERVEUR_V2 / "server_v2.py"), "--auth", "totp", "--port", str(self.port), "--data", str(self.data)],
                                     cwd=str(SERVEUR_V2), env=env, stdout=self.sortie, stderr=self.sortie, stdin=subprocess.DEVNULL)
        attendre(lambda: http_json(self.port, "GET", "/api/health", delai=1)[0] == 200, 15, message="server_v2 ne démarre pas")

    def couper(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None
            self.sortie.close()

    def nettoyer(self):
        self.couper()
        shutil.rmtree(self.data, ignore_errors=True)

    def ecrire(self, methode, chemin, corps=None):
        return http_json(self.port, methode, chemin, corps, {"X-Code": totp.code(self.secret)})

    def etat(self):
        statut, valeur, _ = self.ecrire("GET", "/api/etat")
        assert statut == 200, statut
        return valeur

    def admin(self, chemin, corps, mot_de_passe):
        statut, valeur, en_tetes = http_json(self.port, "POST", "/api/admin/login", {"user": "will", "password": mot_de_passe})
        assert statut == 200, (statut, valeur)
        cookie = en_tetes["Set-Cookie"].split(";", 1)[0]
        return http_json(self.port, "POST", chemin, corps, {"Cookie": cookie})


def charger_admin():
    spec = importlib.util.spec_from_file_location("badaboum_admin_fixture", SERVEUR_V2 / "admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- unitaires

class TestModules(unittest.TestCase):
    def test_totp_copie_conforme_du_serveur(self):
        if not SERVEUR_PRESENT:
            self.skipTest("server_v2 absent")
        ici = Path(totp.__file__).read_bytes()
        self.assertEqual(ici, (SERVEUR_V2 / "totp.py").read_bytes())

    def test_totp_vecteur_rfc6238(self):
        # Vecteur SHA-1 de la RFC 6238 (clé ASCII « 12345678901234567890 »), 6 derniers chiffres.
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(totp.code(secret, 59), "287082")
        self.assertEqual(totp.code(secret, 1111111109), "081804")

    def test_coffre_fichier_600_et_valeurs_par_defaut(self):
        reinitialiser_coffre()
        r = coffre.lire()
        self.assertEqual(r["cibles"], ["https://tech.ame.paris"])
        self.assertEqual((r["port_pp"], r["mode"], r["canal_maj"]), (0, "auto", "stable"))
        self.assertFalse(r["secret_totp"])
        cid = r["client_id"]
        secret = totp.nouveau_secret()
        coffre.ecrire({"secret_totp": secret, "port_pp": 50002})
        r2 = coffre.lire()
        self.assertEqual((r2["secret_totp"], r2["port_pp"], r2["client_id"]), (secret, 50002, cid))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(coffre.chemin().stat().st_mode), 0o600)
        coffre.ecrire({"mode": "nimporte", "port_pp": 99999})
        self.assertEqual((coffre.lire()["mode"], coffre.lire()["port_pp"]), ("auto", 0))
        reinitialiser_coffre()

    def test_journal_masque_secret_code_et_cookie(self):
        secret = totp.nouveau_secret()
        journal.enregistrer_sensible(secret)
        journal.log("info", "essai", "secret " + secret + " code 482913 Cookie: badaboum_session=abc.def AMETECH1:" + secret,
                    code="482913", detail={"cookie": "x", "url": "otpauth://totp/x?secret=" + secret})
        journal.log("info", "essai", "port 48690 et heure 12:30:15 restent lisibles")
        texte = json.dumps(journal.dernieres(5), ensure_ascii=False)
        for interdit in (secret, "482913", "abc.def"):
            self.assertNotIn(interdit, texte)
        self.assertIn("48690", texte)
        self.assertIn("12:30:15", texte)
        z = zipfile.ZipFile(io.BytesIO(journal.rapport_zip({"essai": True})))
        tout = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        self.assertNotIn(secret, tout)
        self.assertNotIn("482913", tout)
        self.assertIn("journal-memoire.jsonl", z.namelist())

    def test_contenu_chant_verset_et_off(self):
        self.assertEqual(pp.contenu_depuis({"active": True, "text": "Jean 3:16\nCar Dieu a tant aimé le monde"}).genre, "verset")
        c = pp.contenu_depuis({"active": True, "text": "Tu es digne\nde louange"})
        self.assertEqual((c.genre, c.texte), ("chant", "Tu es digne\nde louange"))
        self.assertEqual(pp.contenu_depuis({"active": False, "text": "x"}).genre, "off")
        self.assertEqual(pp.contenu_depuis({"active": True, "text": "x", "notes": "badaboum:off"}).genre, "off")

    def test_objets_flux_coupes_octet_par_octet(self):
        brut = ('{"url":"status/slide","data":{"current":{"text":"Grâce 🎤"}}}\r\n\r\n' 'data: {"url":"status/layers","data":{"slide":true}}\r\n\r\n').encode()
        objets = list(pp.objets_flux(bytes([b]) for b in brut))
        self.assertEqual([o["url"] for o in objets], ["status/slide", "status/layers"])

    def test_decouverte_du_faux_propresenter(self):
        faux = FauxProPresenter()
        try:
            port, desc = pp.decouvrir(faux.port)
            self.assertEqual(port, faux.port)
            self.assertIn("ProPresenter", desc)
            self.assertEqual(pp.version_depuis(desc), "7.16.2")
        finally:
            faux.fermer()


# ---------------------------------------------------------------- bout en bout

@unittest.skipUnless(SERVEUR_PRESENT, "server_v2.py introuvable")
class TestBoutEnBout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_mod = charger_admin()
        cls.mot_de_passe = secrets.token_urlsafe(18)
        cls.secret = totp.nouveau_secret()
        empreinte = cls.admin_mod.empreinte(cls.mot_de_passe)
        cls.a = ServeurCible(cls.secret, empreinte)
        cls.b = ServeurCible(cls.secret, empreinte)
        cls.a.lancer()
        cls.b.lancer()
        for s in (cls.a, cls.b):
            statut, valeur, _ = s.ecrire("POST", "/api/etat", {"source": "presenter"})
            assert statut == 200, (statut, valeur)
        cls.faux = FauxProPresenter()
        # Même poste d'un test à l'autre : sinon le serveur refuse un second relais tant que le bail court (occupied).
        cls.client_id = secrets.token_hex(16)

    @classmethod
    def tearDownClass(cls):
        cls.faux.fermer()
        cls.a.nettoyer()
        cls.b.nettoyer()

    def setUp(self):
        if self.a.proc is None:
            self.a.lancer()
        for s in (self.a, self.b):
            s.ecrire("POST", "/api/etat", {"source": "presenter"})
        reinitialiser_coffre(secret_totp=self.secret, cibles=[self.a.url, self.b.url], port_pp=self.faux.port, client_id=self.client_id)
        self.faux.eteindre()
        self.relais = None

    def tearDown(self):
        if self.relais:
            self.relais.arreter()
        # Laisser expirer le bail côté serveur pour que le test suivant parte d'un état neutre.
        for s in (self.a, self.b):
            if s.proc:
                s.ecrire("POST", "/api/etat", {"chant": {"on": False}, "verset": {"on": False}, "source": "presenter"})

    def nouveau_relais(self, **reglages_temps):
        r = Relais()
        r.DELAI_MAX = 1.0
        r.BATTEMENT_S = 0.5
        for k, v in reglages_temps.items():
            setattr(r, k, v)
        self.relais = r
        return r

    def chant_sur(self, serveur, texte):
        def ok():
            e = serveur.etat()
            return e["chant"]["on"] and e["chant"]["ligne"] == texte and e
        return ok

    def test_enrolement_par_admin_puis_login_par_code(self):
        statut, valeur, _ = self.a.admin("/api/admin/totp/afficher", {"password": self.mot_de_passe}, self.mot_de_passe)
        self.assertEqual(statut, 200)
        enrolement = valeur["enrolement"]
        self.assertTrue(enrolement.startswith("AMETECH1:"))
        reinitialiser_coffre(cibles=[self.a.url], client_id=self.client_id)
        r = self.nouveau_relais()
        ui = ui_server.ServeurUI(r, www=Path(DOSSIER_TEST) / "www-absent")
        ui.demarrer(0)
        ui.servir_en_fond()
        try:
            h = {"X-Jeton": ui.jeton}
            self.assertEqual(http_json(ui.port, "GET", "/api/code")[0], 403)
            self.assertEqual(http_json(ui.port, "POST", "/api/enroler", {"texte": "n'importe quoi"}, h)[0], 400)
            self.assertEqual(http_json(ui.port, "POST", "/api/enroler", {"texte": enrolement}, h)[0], 200)
            self.assertEqual(coffre.lire()["secret_totp"], self.secret)
            statut, code, _ = http_json(ui.port, "GET", "/api/code")
            self.assertEqual(statut, 200)
            self.assertRegex(code["code"], r"^\d{6}$")
            self.assertTrue(1 <= code["reste_s"] <= 30)
            self.assertTrue(totp.verifier(self.secret, code["code"]))
            self.assertNotIn(self.secret, json.dumps(journal.dernieres(2000)))
        finally:
            ui.arreter()
        c = Cible(self.a.url, lambda: coffre.lire()["secret_totp"])
        c.login()
        self.assertTrue(c.connecte())
        self.assertIsNotNone(c.ecart_s)
        self.assertLess(abs(c.ecart_s), 2)
        self.assertEqual(c.requete("GET", "/api/config")["version"], 2)

    def test_texte_de_bout_en_bout_puis_extinction(self):
        r = self.nouveau_relais()
        r.demarrer()
        attendre(lambda: r.etat()["relais"]["phase"] == "en_ligne", 10, message="relais pas en ligne")
        self.faux.changer("Grâce infinie\nqui me relève")
        e = attendre(self.chant_sur(self.a, "Grâce infinie\nqui me relève"), 8, message="texte absent du serveur A")
        self.assertTrue(e["presenter"]["online"])
        etat = r.etat()
        self.assertEqual(etat["relais"]["cible"], {"url": self.a.url, "connectee": True})
        self.assertEqual(etat["relais"]["pp"]["port"], self.faux.port)
        self.assertEqual(etat["relais"]["dernier_texte"], "Grâce infinie\nqui me relève")
        self.assertIsNotNone(etat["relais"]["dernier_envoi"])
        self.assertTrue(etat["horloge"]["ok"])
        self.assertIsNotNone(etat["horloge"]["ecart_s"])
        self.faux.changer("Jean 3:16\nCar Dieu a tant aimé le monde")
        def verset():
            v = self.a.etat()["verset"]
            return v["on"] and v["ref"] == "Jean 3:16" and v
        attendre(verset, 8, message="verset absent")
        self.faux.eteindre()
        attendre(lambda: not self.a.etat()["verset"]["on"] and not self.a.etat()["chant"]["on"], 8, message="texte non éteint")
        # Arrêt du relais : trame vide envoyée, le serveur voit le relais partir.
        self.faux.changer("Dernier texte")
        attendre(self.chant_sur(self.a, "Dernier texte"), 8)
        r.arreter()
        attendre(lambda: not self.a.etat()["chant"]["on"], 4, message="texte resté après l'arrêt")
        self.assertEqual(r.etat()["relais"]["phase"], "arret")

    def test_bascule_vers_secours_puis_retour(self):
        r = self.nouveau_relais(RETOUR_PREMIERE_S=2.0)
        r.demarrer()
        self.faux.changer("Avant la panne")
        attendre(self.chant_sur(self.a, "Avant la panne"), 10, message="texte absent de A")
        self.a.couper()
        self.faux.changer("Pendant la panne")
        attendre(self.chant_sur(self.b, "Pendant la panne"), 15, message="pas de bascule vers B")
        self.assertEqual(r.etat()["relais"]["cible"]["url"], self.b.url)
        lignes = json.dumps(journal.dernieres(200), ensure_ascii=False)
        self.assertIn("bascule vers " + self.b.url, lignes)
        self.a.lancer()
        self.a.ecrire("POST", "/api/etat", {"source": "presenter"})
        attendre(lambda: r.etat()["relais"]["cible"]["url"] == self.a.url, 15, message="pas de retour sur A")
        self.faux.changer("Après le retour")
        attendre(self.chant_sur(self.a, "Après le retour"), 10, message="texte absent de A après retour")
        # B a reçu une trame vide en partant : plus de texte de secours en vol.
        attendre(lambda: not self.b.etat()["chant"]["on"], 10, message="B garde un vieux texte")

    def test_manuel_garde_la_connexion_sans_envoyer_de_texte(self):
        r = self.nouveau_relais()
        r.demarrer()
        self.faux.changer("Texte automatique")
        attendre(self.chant_sur(self.a, "Texte automatique"), 10)
        r.definir_mode("manuel")
        attendre(lambda: self.a.etat()["source"] == "manual", 5, message="source serveur non passée en manuel")
        self.assertEqual(coffre.lire()["mode"], "manuel")
        seq = self.a.etat()["presenter"]["sequence"]
        self.faux.changer("Ne doit pas partir")
        time.sleep(1.5)
        e = self.a.etat()
        self.assertGreater(e["presenter"]["sequence"], seq)  # toujours connecté
        self.assertTrue(e["presenter"]["online"])
        self.assertNotEqual(e["chant"]["ligne"], "Ne doit pas partir")
        etat = r.etat()
        self.assertEqual((etat["mode"], etat["relais"]["phase"], etat["relais"]["dernier_texte"]), ("manuel", "en_ligne", ""))
        r.definir_mode("auto")
        attendre(self.chant_sur(self.a, "Ne doit pas partir"), 8, message="texte non repris en Auto")
        self.assertEqual(self.a.etat()["source"], "presenter")

    def test_mode_suit_la_regie(self):
        r = self.nouveau_relais()
        r.demarrer()
        attendre(lambda: r.etat()["relais"]["phase"] == "en_ligne", 10)
        self.a.ecrire("POST", "/api/etat", {"source": "manual"})
        attendre(lambda: r.etat()["mode"] == "manuel", 5, message="mode non repris depuis la régie")
        self.a.ecrire("POST", "/api/etat", {"source": "presenter"})
        attendre(lambda: r.etat()["mode"] == "auto", 5)

    def test_401_relance_un_login(self):
        r = self.nouveau_relais()
        r.demarrer()
        self.faux.changer("Avant la coupure")
        attendre(self.chant_sur(self.a, "Avant la coupure"), 10)
        ouvertes = lambda: sum("Session ouverte sur " + self.a.url in l["message"] for l in journal.dernieres(2000))
        avant = ouvertes()
        statut, _, _ = self.a.admin("/api/admin/sessions/couper", {}, self.mot_de_passe)
        self.assertEqual(statut, 200)
        attendre(lambda: ouvertes() > avant, 8, message="pas de nouveau login après 401")
        self.assertTrue(any("401" in l["message"] for l in journal.dernieres(200)))
        self.faux.changer("Après la coupure")
        attendre(self.chant_sur(self.a, "Après la coupure"), 8)
        self.assertEqual(r.etat()["relais"]["cible"]["url"], self.a.url)  # un 401 ne fait pas basculer

    def test_sans_propresenter_rien_ne_part(self):
        reinitialiser_coffre(secret_totp=self.secret, cibles=[self.a.url], port_pp=port_libre(), client_id=self.client_id)
        r = self.nouveau_relais()
        r.demarrer()
        attendre(lambda: r.etat()["relais"]["phase"] == "recherche_pp", 10)
        attendre(lambda: r.etat()["relais"]["cible"]["connectee"], 10)
        self.assertFalse(self.a.etat()["chant"]["on"])

    def test_code_refuse_passe_en_erreur(self):
        reinitialiser_coffre(secret_totp=totp.nouveau_secret(), cibles=[self.b.url], port_pp=self.faux.port, client_id=self.client_id)
        r = self.nouveau_relais()
        r.demarrer()
        attendre(lambda: r.etat()["relais"]["phase"] == "erreur", 10, message="code faux non signalé")
        self.assertIn("Code refusé", r.etat()["relais"]["message"])


# ---------------------------------------------------------------- serveur cible simulé

class FausseCible:
    """Serveur cible minimal : compte les logins, statut de login réglable (200, 401, 429)."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.verrou = threading.Lock()
        self.statut_login = 200
        self.logins = 0
        self.claims = 0
        self.trames: list[dict] = []
        self.etats: list[dict] = []
        self.source = "presenter"
        cible = self

        class Gestion(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def repondre(self, statut, corps, entetes=None):
                brut = json.dumps(corps).encode()
                self.send_response(statut)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(brut)))
                for k, v in (entetes or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(brut)

            def do_GET(self):
                if self.path == "/api/heure":
                    return self.repondre(200, {"t": time.time()})
                if self.path == "/api/session":
                    return self.repondre(200, {"pin_requis": False, "mode": "totp"})
                return self.repondre(404, {"erreur": "route"})

            def do_POST(self):
                longueur = int(self.headers.get("Content-Length") or 0)
                corps = json.loads(self.rfile.read(longueur) or b"{}")
                with cible.verrou:
                    if self.path == "/api/login":
                        cible.logins += 1
                        statut, n = cible.statut_login, cible.logins
                        if statut == 200:
                            return self.repondre(200, {"ok": True}, {"Set-Cookie": "badaboum_session=c%d; Path=/; HttpOnly; Max-Age=43200" % n})
                        return self.repondre(statut, {"erreur": "Code incorrect"})
                    if "badaboum_session=" not in (self.headers.get("Cookie") or ""):
                        return self.repondre(401, {"erreur": "authentification"})
                    if self.path == "/api/presenter/claim":
                        cible.claims += 1
                        return self.repondre(200, {"session": "s%d" % cible.claims, "server_time": time.time()})
                    if self.path == "/api/presenter/frame":
                        cible.trames.append(corps)
                        return self.repondre(200, {"ok": True, "source": cible.source})
                    if self.path == "/api/etat":
                        cible.etats.append(corps)
                        if corps.get("source") in ("presenter", "manual"):
                            cible.source = corps["source"]
                        return self.repondre(200, {})
                return self.repondre(404, {"erreur": "route"})

        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), Gestion)
        self.serveur.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.serveur.server_address[1])
        threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def nb_logins(self) -> int:
        with self.verrou:
            return self.logins

    def fermer(self):
        self.serveur.shutdown()
        self.serveur.server_close()


class TestRelaisCibleSimulee(unittest.TestCase):
    """Comportements du relais face au serveur : refus du code, blocage 429, renouvellement, mode."""

    @classmethod
    def setUpClass(cls):
        cls.faux = FauxProPresenter()

    @classmethod
    def tearDownClass(cls):
        cls.faux.fermer()

    def setUp(self):
        self.cible = FausseCible()
        self.addCleanup(self.cible.fermer)
        self.faux.eteindre()
        reinitialiser_coffre(secret_totp=totp.nouveau_secret(), cibles=[self.cible.url], port_pp=self.faux.port)
        self.relais = Relais()
        self.relais.DELAI_MAX = 0.5
        self.relais.BATTEMENT_S = 0.2
        self.addCleanup(self.relais.arreter)

    def client(self):
        return self.relais._client(self.cible.url)

    def vieillir_session(self, secondes):
        c = self.client()
        with c._verrou:
            c._ouvert -= secondes
            c._expire -= secondes
        self.relais._reveil.set()

    # --- point 1 : un vieux secret ne doit pas entretenir le blocage de l'adresse IP
    def test_deux_refus_du_code_arretent_les_essais(self):
        self.cible.statut_login = 401
        self.relais.ATTENTE_CODE_REFUSE = 0.2
        self.relais.demarrer()
        attendre(lambda: "deux fois" in self.relais.etat()["relais"]["message"], 10, message="pas d'arrêt après deux refus")
        etat = self.relais.etat()["relais"]
        self.assertEqual(etat["phase"], "erreur")
        self.assertEqual(etat["message"], "Code refusé deux fois : réenrôlez ce poste (code fourni par l'administrateur)")
        time.sleep(1.5)
        self.assertEqual(self.cible.nb_logins(), 2)
        d = self.relais.diagnostic()
        session = [e for e in d["etapes"] if e["nom"] == "session"][0]
        self.assertFalse(session["ok"])
        self.assertIn("réenrôlez", session["detail"])
        self.assertEqual(self.cible.nb_logins(), 2)  # le diagnostic n'a pas consommé d'essai
        # Nouvel enrôlement : les essais reprennent.
        self.cible.statut_login = 200
        coffre.ecrire({"secret_totp": totp.nouveau_secret()})
        self.relais.oublier_sessions()
        attendre(lambda: self.relais.etat()["relais"]["cible"]["connectee"], 10, message="pas de reprise après réenrôlement")

    def test_redemarrage_manuel_relance_les_essais(self):
        self.cible.statut_login = 401
        self.relais.ATTENTE_CODE_REFUSE = 0.2
        self.relais.demarrer()
        attendre(lambda: "deux fois" in self.relais.etat()["relais"]["message"], 10)
        self.relais.arreter()
        self.cible.statut_login = 200
        self.relais.demarrer()
        attendre(lambda: self.relais.etat()["relais"]["cible"]["connectee"], 10, message="pas de reprise après redémarrage")

    def test_diagnostic_sans_essai_pendant_un_refus(self):
        self.cible.statut_login = 401
        self.relais.ATTENTE_CODE_REFUSE = 60
        self.relais.demarrer()
        attendre(lambda: self.cible.nb_logins() == 1 and self.relais.etat()["relais"]["phase"] == "erreur", 10)
        d = self.relais.diagnostic()
        session = [e for e in d["etapes"] if e["nom"] == "session"][0]
        self.assertFalse(session["ok"])
        self.assertEqual(self.cible.nb_logins(), 1)

    def test_pas_de_login_pendant_15_min_apres_un_429(self):
        self.assertGreaterEqual(Relais.ATTENTE_BLOQUE, 900)
        self.cible.statut_login = 429
        self.relais.demarrer()
        attendre(lambda: self.cible.nb_logins() == 1 and self.relais.etat()["relais"]["phase"] == "erreur", 10)
        d = self.relais.diagnostic()
        self.assertFalse([e for e in d["etapes"] if e["nom"] == "session"][0]["ok"])
        # Même un redémarrage manuel n'envoie pas de code avant la fin du blocage.
        self.relais.arreter()
        self.relais.demarrer()
        time.sleep(1.0)
        self.assertEqual(self.cible.nb_logins(), 1)

    # --- point 2 : renouvellement anticipé du cookie
    def test_cible_renouvellement_du(self):
        from ametech import cible as cible_mod
        c = cible_mod.Cible(self.cible.url, lambda: totp.nouveau_secret())
        self.assertFalse(c.renouvellement_du(True))  # pas de session
        c.login()
        self.assertFalse(c.renouvellement_du(True))
        with c._verrou:
            c._ouvert -= 11 * 3600 + 1
            c._expire -= 11 * 3600 + 1
        self.assertTrue(c.connecte())
        self.assertTrue(c.renouvellement_du(True))
        self.assertFalse(c.renouvellement_du(False))  # un texte passe : on attend
        with c._verrou:
            c._ouvert -= 31 * 60
            c._expire -= 31 * 60
        self.assertTrue(c.renouvellement_du(False))  # 30 min avant l'expiration : on renouvelle quand même

    def test_renouvellement_anticipe_au_calme(self):
        self.relais.demarrer()
        attendre(lambda: self.relais.etat()["relais"]["cible"]["connectee"], 10)
        self.assertEqual(self.cible.nb_logins(), 1)
        self.vieillir_session(11 * 3600 + 1)
        attendre(lambda: self.cible.nb_logins() == 2, 5, message="pas de renouvellement à 11 h")
        self.assertEqual(self.cible.claims, 1)  # la session du relais n'est pas coupée
        self.assertTrue(self.relais.etat()["relais"]["cible"]["connectee"])
        time.sleep(0.6)
        self.assertEqual(self.cible.nb_logins(), 2)  # un seul renouvellement

    def test_pas_de_renouvellement_pendant_un_passage(self):
        self.relais.demarrer()
        self.faux.changer("Texte à l'antenne")
        attendre(lambda: self.relais.etat()["relais"]["dernier_envoi"], 10, message="texte non transmis")
        self.vieillir_session(11 * 3600 + 1)
        time.sleep(1.0)
        self.assertEqual(self.cible.nb_logins(), 1)
        self.vieillir_session(31 * 60)
        attendre(lambda: self.cible.nb_logins() == 2, 5, message="pas de renouvellement 30 min avant l'expiration")
        self.assertEqual(self.cible.claims, 1)

    # --- point 6 : choix Auto / Manuel fait relais arrêté
    def test_mode_choisi_relais_arrete_pousse_au_demarrage(self):
        self.relais.definir_mode("manuel")
        self.relais.demarrer()
        attendre(lambda: self.cible.source == "manual", 8, message="mode Manuel non poussé au serveur")
        time.sleep(0.6)
        self.assertEqual(self.relais.etat()["mode"], "manuel")
        self.assertEqual(self.cible.etats[-1], {"source": "manual"})


# ---------------------------------------------------------------- lancement et relance après mise à jour

SCRIPT_LANCEMENT = r'''
import json, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
SCENARIO = json.loads(sys.argv[2])
TRACE = sys.argv[3]
from ametech import __main__ as principal, ui_server

def tracer(quoi):
    with open(TRACE, "a", encoding="utf-8") as f:
        f.write(quoi + "\n")

class FauxRelais:
    def __init__(self):
        self.actif = SCENARIO.get("relais_actif", False)
    def demarrer(self):
        self.actif = True
        tracer("demarrer")
    def arreter(self, journaliser=True):
        self.actif = False
    def etat(self):
        return {"relais": {"phase": "en_ligne" if self.actif else "arret"}}

class FauxUI:
    def __init__(self, relais, *a, **k):
        self.fin = threading.Event()
        self.url = "http://127.0.0.1:1/"
    def demarrer(self, port=None):
        pass
    def servir(self):
        if SCENARIO.get("quitter"):
            return
        self.fin.wait(20)
    def arreter(self):
        self.fin.set()
        time.sleep(0.5)  # le fil principal sort de servir() avant la fin de l'arrêt

class FauxMaj:
    def definir_arret(self, fonction):
        self.fonction = fonction
    def etat(self):
        return {"statut": SCENARIO.get("statut_maj", "a_jour")}
    def boucle(self, relais):
        if SCENARIO.get("relance"):
            time.sleep(0.3)
            self.fonction(75)

principal.Relais = FauxRelais
ui_server.ServeurUI = FauxUI
ui_server.maj = FauxMaj()
ui_server.instance_active = lambda: None
ui_server.ouvrir_fenetre = lambda url: None
sys.exit(principal.main(["--sans-fenetre"]))
'''


class TestLancement(unittest.TestCase):
    """__main__ : code de sortie 75 garanti, relais relancé après une mise à jour."""

    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix="ametech-lancement-"))
        self.addCleanup(shutil.rmtree, self.dossier, True)
        self.script = self.dossier / "lancer.py"
        self.script.write_text(SCRIPT_LANCEMENT, encoding="utf-8")
        self.trace = self.dossier / "trace.txt"
        self.env = {**os.environ, "AMETECH_DOSSIER": str(self.dossier / "reglages")}
        self.ecrire_reglages(secret_totp=totp.nouveau_secret())

    def ecrire_reglages(self, **champs):
        ancien = os.environ.get("AMETECH_DOSSIER")
        os.environ["AMETECH_DOSSIER"] = self.env["AMETECH_DOSSIER"]
        try:
            coffre.ecrire(champs)
        finally:
            os.environ["AMETECH_DOSSIER"] = ancien

    def lire_reglages(self) -> dict:
        ancien = os.environ.get("AMETECH_DOSSIER")
        os.environ["AMETECH_DOSSIER"] = self.env["AMETECH_DOSSIER"]
        try:
            return coffre.lire()
        finally:
            os.environ["AMETECH_DOSSIER"] = ancien

    def lancer(self, **scenario) -> tuple[int, list[str]]:
        try:
            self.trace.unlink()
        except FileNotFoundError:
            pass
        windows = str(Path(__file__).resolve().parents[2])
        p = subprocess.run([sys.executable, str(self.script), windows, json.dumps(scenario), str(self.trace)],
                           env=self.env, capture_output=True, timeout=30)
        traces = self.trace.read_text(encoding="utf-8").split() if self.trace.exists() else []
        return p.returncode, traces

    def test_code_75_gagne_toujours(self):
        code, _ = self.lancer(relance=True)
        self.assertEqual(code, 75)

    def test_quitter_pendant_une_relance_en_attente_sort_en_75(self):
        code, _ = self.lancer(quitter=True, statut_maj="pret")
        self.assertEqual(code, 75)
        code, _ = self.lancer(quitter=True, statut_maj="a_jour")
        self.assertEqual(code, 0)

    def test_relais_relance_apres_la_mise_a_jour(self):
        code, traces = self.lancer(relance=True, relais_actif=True)
        self.assertEqual(code, 75)
        self.assertNotIn("demarrer", traces)
        self.assertTrue(self.lire_reglages()["relais_apres_maj"])
        # Nouvelle version : le relais repart tout seul, puis la marque est effacée.
        code, traces = self.lancer(quitter=True)
        self.assertEqual((code, traces), (0, ["demarrer"]))
        self.assertFalse(self.lire_reglages()["relais_apres_maj"])
        code, traces = self.lancer(quitter=True)
        self.assertEqual(traces, [])

    def test_relais_arrete_reste_arrete_apres_la_mise_a_jour(self):
        code, _ = self.lancer(relance=True, relais_actif=False)
        self.assertEqual(code, 75)
        self.assertFalse(self.lire_reglages()["relais_apres_maj"])
        self.assertEqual(self.lancer(quitter=True)[1], [])


# ---------------------------------------------------------------- API locale

class TestApiLocale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.faux = FauxProPresenter()
        cls.www = Path(tempfile.mkdtemp(prefix="ametech-www-"))
        (cls.www / "index.html").write_text('<!doctype html><html><head><script>window.AMETECH_JETON="{{JETON}}";</script>'
                                            '<link rel="stylesheet" href="app.css"></head><body>AME</body></html>', encoding="utf-8")
        (cls.www / "app.css").write_text("body{color:red}")
        (cls.www / "assets").mkdir()
        (cls.www / "assets" / "qr.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")

    @classmethod
    def tearDownClass(cls):
        cls.faux.fermer()
        shutil.rmtree(cls.www, ignore_errors=True)

    def setUp(self):
        reinitialiser_coffre(cibles=["http://127.0.0.1:" + str(port_libre())], port_pp=self.faux.port)
        self.relais = Relais()
        self.ui = ui_server.ServeurUI(self.relais, www=self.www)
        self.ui.demarrer(0)
        self.fil = self.ui.servir_en_fond()
        self.port = self.ui.port
        self.h = {"X-Jeton": self.ui.jeton}

    def tearDown(self):
        self.relais.arreter()
        self.ui.arreter()

    def test_index_jeton_injecte_et_csp(self):
        statut, corps, en_tetes = http_json(self.port, "GET", "/")
        self.assertEqual(statut, 200)
        html = corps.decode()
        self.assertIn('window.AMETECH_JETON="' + self.ui.jeton + '"', html)
        self.assertNotIn("{{JETON}}", html)
        self.assertIn("'sha256-", en_tetes["Content-Security-Policy"])
        self.assertEqual(en_tetes["Cache-Control"], "no-store")
        statut, corps, en_tetes = http_json(self.port, "GET", "/app.css")
        self.assertEqual((statut, en_tetes["Content-Type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(http_json(self.port, "GET", "/assets/qr.svg")[2]["Content-Type"], "image/svg+xml")
        self.assertEqual(http_json(self.port, "GET", "/../coffre.py")[0], 403)
        self.assertEqual(http_json(self.port, "GET", "/absent.js")[0], 404)

    def test_jeton_hote_et_origine_exiges(self):
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "auto"})[0], 403)
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "auto"}, {"X-Jeton": "faux"})[0], 403)
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "auto"}, self.h)[0], 200)
        self.assertEqual(http_json(self.port, "GET", "/api/etat", entetes={"Host": "evil.example:" + str(self.port)})[0], 403)
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "auto"}, {**self.h, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(http_json(self.port, "GET", "/api/code", entetes={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_etat_suit_le_contrat(self):
        statut, e, en_tetes = http_json(self.port, "GET", "/api/etat")
        self.assertEqual(statut, 200)
        self.assertEqual(en_tetes["Cache-Control"], "no-store")
        for cle in ("version", "relais", "mode", "enrole", "horloge", "maj", "assistance"):
            self.assertIn(cle, e)
        for cle in ("actif", "phase", "message", "pp", "cible", "dernier_texte", "depuis"):
            self.assertIn(cle, e["relais"])
        self.assertEqual(set(e["relais"]["cible"]), {"url", "connectee"})
        self.assertEqual(set(e["horloge"]), {"ecart_s", "ok"})
        self.assertEqual(e["relais"]["phase"], "arret")
        self.assertFalse(e["enrole"])
        self.assertIn("statut", e["maj"])
        self.assertIn("rustdesk", e["assistance"])

    def test_enroler_code_oublier(self):
        self.assertEqual(http_json(self.port, "GET", "/api/code")[0], 403)
        secret = totp.nouveau_secret()
        self.assertEqual(http_json(self.port, "POST", "/api/enroler", {"texte": totp.uri(secret)}, self.h)[0], 200)
        statut, c, _ = http_json(self.port, "GET", "/api/code")
        self.assertEqual(statut, 200)
        self.assertTrue(totp.verifier(secret, c["code"]))
        self.assertTrue(http_json(self.port, "GET", "/api/etat")[1]["enrole"])
        self.assertEqual(http_json(self.port, "POST", "/api/oublier", None, self.h)[1], {"ok": True})
        self.assertFalse(coffre.lire()["secret_totp"])
        self.assertEqual(http_json(self.port, "GET", "/api/code")[0], 403)
        self.assertNotIn(secret, json.dumps(http_json(self.port, "GET", "/api/journal?depuis=0")[1]))

    def test_mode_relais_et_journal(self):
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "autre"}, self.h)[0], 400)
        self.assertEqual(http_json(self.port, "POST", "/api/mode", {"mode": "manuel"}, self.h)[0], 200)
        self.assertEqual(http_json(self.port, "GET", "/api/etat")[1]["mode"], "manuel")
        self.assertEqual(http_json(self.port, "POST", "/api/relais/demarrer", None, self.h)[1], {"ok": True})
        attendre(lambda: http_json(self.port, "GET", "/api/etat")[1]["relais"]["actif"], 3)
        self.assertEqual(http_json(self.port, "POST", "/api/relais/arreter", None, self.h)[1], {"ok": True})
        self.assertEqual(http_json(self.port, "GET", "/api/etat")[1]["relais"]["phase"], "arret")
        statut, j, _ = http_json(self.port, "GET", "/api/journal?depuis=0")
        self.assertEqual(statut, 200)
        self.assertTrue(j["lignes"])
        self.assertEqual(set(j["lignes"][-1]), {"n", "t", "niveau", "etape", "message"})
        self.assertEqual(http_json(self.port, "GET", "/api/journal?depuis=" + str(j["dernier"]))[1]["lignes"], [])

    def test_reglages(self):
        statut, r, _ = http_json(self.port, "GET", "/api/reglages")
        self.assertEqual(set(r), {"cibles", "port_pp", "demarrage_windows", "relais_au_lancement"})
        self.assertNotIn("secret_totp", json.dumps(r))
        self.assertEqual(http_json(self.port, "POST", "/api/reglages", {"cibles": ["http://exemple.org"]}, self.h)[0], 400)
        self.assertEqual(http_json(self.port, "POST", "/api/reglages", {"port_pp": 70000}, self.h)[0], 400)
        statut, r, _ = http_json(self.port, "POST", "/api/reglages", {"cibles": ["https://tech.ame.paris", "https://secours.example"], "relais_au_lancement": True}, self.h)
        self.assertEqual(statut, 200)
        self.assertEqual(r["cibles"], ["https://tech.ame.paris", "https://secours.example"])
        self.assertTrue(coffre.lire()["relais_au_lancement"])

    def test_propresenter_info_requete_et_flux(self):
        self.faux.changer("Bonjour")
        statut, info, _ = http_json(self.port, "GET", "/api/pp/info")
        self.assertEqual(statut, 200)
        self.assertEqual((info["port"], info["version"]), (self.faux.port, "7.16.2"))
        self.assertIn("slide", info["actives"])
        self.assertEqual(http_json(self.port, "POST", "/api/pp/requete", {"methode": "GET", "chemin": "/version"}, self.h)[0], 400)
        self.assertEqual(http_json(self.port, "POST", "/api/pp/requete", {"methode": "DELETE", "chemin": "/v1/x"}, self.h)[0], 400)
        self.assertEqual(http_json(self.port, "POST", "/api/pp/requete", {"methode": "GET", "chemin": "/v1/../version"}, self.h)[0], 400)
        statut, rep, _ = http_json(self.port, "POST", "/api/pp/requete", {"methode": "GET", "chemin": "/v1/status/slide"}, self.h)
        self.assertEqual((statut, rep["statut"]), (200, 200))
        self.assertEqual(rep["corps"]["current"]["text"], "Bonjour")
        statut, rep, _ = http_json(self.port, "POST", "/api/pp/requete", {"methode": "PUT", "chemin": "/v1/stage/message", "corps": {"a": 1}}, self.h)
        self.assertEqual(rep["corps"], {"recu": {"a": 1}})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/pp/flux")
        r = conn.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        recu = b""
        while b'"status/slide"' not in recu:
            recu += r.fp.readline()
        conn.close()
        self.assertIn("Bonjour", recu.decode())

    def test_diagnostic_et_rapport(self):
        statut, d, _ = http_json(self.port, "GET", "/api/diagnostic", delai=30)
        self.assertEqual(statut, 200)
        noms = [e["nom"] for e in d["etapes"]]
        self.assertEqual(noms, ["ProPresenter", "Internet", "serveur", "session", "horloge"])
        self.assertTrue(d["etapes"][0]["ok"])
        self.assertFalse(d["etapes"][2]["ok"])  # cible locale inexistante
        secret = totp.nouveau_secret()
        coffre.ecrire({"secret_totp": secret})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/rapport")
        r = conn.getresponse()
        brut = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        z = zipfile.ZipFile(io.BytesIO(brut))
        tout = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        self.assertNotIn(secret, tout)
        self.assertIn('"enrole": true', tout)

    def test_maj_assistance_et_quitter(self):
        from unittest import mock
        # Aucun appel réseau réel : maj et assistance absents, puis remplacés par des doublures.
        with mock.patch.object(ui_server, "maj", None), mock.patch.object(ui_server, "assistance", None):
            statut, rep, _ = http_json(self.port, "POST", "/api/maj/verifier", None, self.h)
            self.assertEqual((statut, rep["statut"]), (503, "inconnu"))
            statut, rep, _ = http_json(self.port, "POST", "/api/assistance/rustdesk", None, self.h)
            self.assertEqual((statut, rep["rustdesk"]), (503, "absent"))
            e = http_json(self.port, "GET", "/api/etat")[1]
            self.assertEqual((e["maj"]["statut"], e["assistance"]["rustdesk"]), ("inconnu", "absent"))
        doublure = mock.Mock()
        doublure.verifier.return_value = {"version_locale": "3.0.0", "version_dispo": None, "statut": "a_jour", "message": "", "verifie_le": 1.0}
        with mock.patch.object(ui_server, "maj", doublure):
            self.assertEqual(http_json(self.port, "POST", "/api/maj/verifier", None, self.h)[1]["statut"], "a_jour")
        self.assertEqual(http_json(self.port, "GET", "/api/instance")[1]["application"], "AME Tech")
        self.assertEqual(http_json(self.port, "POST", "/api/quitter", None, self.h)[1], {"ok": True})
        self.fil.join(5)
        self.assertFalse(self.fil.is_alive())


if __name__ == "__main__":
    unittest.main()
