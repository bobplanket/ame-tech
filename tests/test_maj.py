"""Mise à jour signée : refus, application, repli de source, retour arrière.

Deux serveurs HTTP locaux simulent GitHub (avec la redirection de
releases/latest/download vers le stockage d'objets) et tech.ame.paris/maj.
"""

import base64
import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "windows"))

from ametech import ed25519, maj  # noqa: E402

GRAINE = hashlib.sha256(b"graine des tests unitaires de maj").digest()
PUBLIQUE = ed25519.cle_publique(GRAINE)
AUTRE_GRAINE = hashlib.sha256(b"une autre graine").digest()
CHEMIN_GITHUB = "/bobplanket/ame-tech/releases/latest/download/"


class Serveur:
    """Serveur de fichiers local : chemins -> octets, redirections, erreurs forcées."""

    def __init__(self):
        self.fichiers: dict[str, bytes] = {}
        self.redirections: dict[str, str] = {}
        self.erreurs: dict[str, int] = {}
        self.requetes: list[str] = []
        serveur = self

        class Gestion(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                serveur.requetes.append(self.path)
                if self.path in serveur.erreurs:
                    self.send_error(serveur.erreurs[self.path])
                elif self.path in serveur.redirections:
                    self.send_response(302)
                    self.send_header("Location", serveur.redirections[self.path])
                    self.end_headers()
                elif self.path in serveur.fichiers:
                    corps = serveur.fichiers[self.path]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(corps)))
                    self.end_headers()
                    try:
                        self.wfile.write(corps)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # client qui coupe exprès (limite de taille)
                else:
                    self.send_error(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Gestion)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def fermer(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def fabriquer_release(version="3.0.1", url_zip="", graine=GRAINE, membres=None, alterer=None):
    """(zip, manifeste brut, signature base64). `alterer(manifeste)` modifie avant signature."""
    membres = dict(membres or {
        "AME Tech.exe": b"MZ faux lanceur",
        "version.txt": f"{version}\n".encode(),
        "ametech/__init__.py": b"",
        "ametech/__main__.py": b"print('AME Tech')\n",
        "ametech/version.py": f'VERSION = "{version}"\n'.encode(),
        "ametech/www/index.html": b"<!doctype html>",
        "runtime/python/pythonw.exe": b"MZ faux pythonw",
    })
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, donnees in membres.items():
            z.writestr(nom, donnees)
    octets = tampon.getvalue()
    manifeste = {
        "version": version,
        "date": "2026-11-01T12:00:00Z",
        "notes": "essai",
        "fichiers": [{"chemin": n, "sha256": hashlib.sha256(d).hexdigest(), "url": url_zip} for n, d in membres.items()],
        "zip": {"url": url_zip, "sha256": hashlib.sha256(octets).hexdigest()},
    }
    if alterer:
        alterer(manifeste)
    brut = json.dumps(manifeste, indent=1).encode()
    signature = base64.b64encode(ed25519.signer(graine, brut)) + b"\n"
    return octets, brut, signature


class FauxRelais:
    def __init__(self, phase="arret", dernier_envoi=None, phase_depuis=None):
        self.phase, self.dernier_envoi, self.phase_depuis = phase, dernier_envoi, phase_depuis

    def etat(self):
        r = {"actif": self.phase != "arret", "phase": self.phase, "dernier_envoi": self.dernier_envoi}
        if self.phase_depuis is not None:
            r["phase_depuis"] = self.phase_depuis
        return {"version": "3.0.0", "relais": r}


class BaseMaj(unittest.TestCase):
    def setUp(self):
        self.github = Serveur()
        self.secours = Serveur()
        self.addCleanup(self.github.fermer)
        self.addCleanup(self.secours.fermer)
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        self.racine = Path(self.dossier.name) / "AME Tech"
        self.relances = []

    def publier(self, serveur, prefixe, version="3.0.1", **options):
        """Publie une release sur `serveur` ; le ZIP est servi sous /objets/ (stockage simulé)."""
        url_zip = f"{serveur.base}/objets/AME-Tech-Windows-{version}.zip"
        octets, brut, sig = fabriquer_release(version, url_zip, **options)
        serveur.fichiers[f"/objets/AME-Tech-Windows-{version}.zip"] = octets
        for nom, donnees in (("manifest.json", brut), ("manifest.json.sig", sig)):
            serveur.fichiers[f"/objets/{nom}"] = donnees
            serveur.redirections[prefixe + nom] = f"{serveur.base}/objets/{nom}"
        return octets, brut, sig

    def majeur(self, version="3.0.0", relais=None, **options):
        m = maj.Majeur(racine=self.racine, version_locale=version, cle=PUBLIQUE, autoriser_http_local=True,
                       sources=[self.github.base + CHEMIN_GITHUB, self.secours.base + "/maj/"],
                       relancer=lambda ancienne, nouvelle: self.relances.append((ancienne, nouvelle)), **options)
        m.definir_relais(relais or FauxRelais("arret"))
        return m

    def zip_telecharge(self, serveur):
        return any(r.endswith(".zip") for r in serveur.requetes)

    def assertRienInstalle(self):
        self.assertFalse((self.racine / "courante.txt").exists())
        versions = self.racine / "versions"
        self.assertEqual([p.name for p in versions.iterdir()] if versions.exists() else [], [])
        self.assertEqual(self.relances, [])


class TestVerification(BaseMaj):
    def test_version_superieure_disponible(self):
        self.publier(self.github, CHEMIN_GITHUB)
        etat = self.majeur().verifier()
        self.assertEqual(etat["statut"], "disponible")
        self.assertEqual(etat["version_dispo"], "3.0.1")
        self.assertIsNotNone(etat["verifie_le"])

    def test_signature_d_une_autre_cle_refusee(self):
        self.publier(self.github, CHEMIN_GITHUB, graine=AUTRE_GRAINE)
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertEqual(m.appliquer()["statut"], "erreur")
        self.assertFalse(self.zip_telecharge(self.github))
        self.assertRienInstalle()

    def test_manifeste_modifie_apres_signature_refuse(self):
        self.publier(self.github, CHEMIN_GITHUB)
        brut = self.github.fichiers["/objets/manifest.json"]
        self.github.fichiers["/objets/manifest.json"] = brut.replace(b'"essai"', b'"pirate"')
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertIn("signature invalide", m.etat()["message"])
        m.appliquer()
        self.assertRienInstalle()

    def test_signature_illisible_refusee(self):
        self.publier(self.github, CHEMIN_GITHUB)
        self.github.fichiers["/objets/manifest.json.sig"] = b"pas du base64 !!"
        self.assertEqual(self.majeur().verifier()["statut"], "erreur")

    def test_version_egale_refusee(self):
        self.publier(self.github, CHEMIN_GITHUB, version="3.0.0")
        m = self.majeur("3.0.0")
        self.assertEqual(m.verifier()["statut"], "a_jour")
        m.appliquer()
        self.assertFalse(self.zip_telecharge(self.github))
        self.assertRienInstalle()

    def test_version_inferieure_refusee(self):
        self.publier(self.github, CHEMIN_GITHUB, version="2.9.9")
        m = self.majeur("3.0.0")
        self.assertEqual(m.verifier()["statut"], "a_jour")
        m.appliquer()
        self.assertRienInstalle()

    def test_comparaison_numerique(self):
        self.publier(self.github, CHEMIN_GITHUB, version="3.0.10")
        self.assertEqual(self.majeur("3.0.9").verifier()["statut"], "disponible")

    def test_manifeste_trop_gros_refuse(self):
        self.publier(self.github, CHEMIN_GITHUB, alterer=lambda m: m.update(notes="x" * (600 * 1024)))
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertIn("volumineux", m.etat()["message"])

    def test_cle_de_test_desactive_les_mises_a_jour(self):
        fichier = Path(self.dossier.name) / "cle.txt"
        fichier.write_text("# CLE-DE-TEST\n" + base64.b64encode(PUBLIQUE).decode() + "\n")
        self.publier(self.github, CHEMIN_GITHUB)
        m = maj.Majeur(racine=self.racine, version_locale="3.0.0", chemin_cle=fichier, autoriser_http_local=True,
                       sources=[self.github.base + CHEMIN_GITHUB], relancer=lambda a, n: self.relances.append(1))
        m.definir_relais(FauxRelais())
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertEqual(self.github.requetes, [])

    def test_vraie_cle_du_depot_est_lisible(self):
        cle, _ = maj.charger_cle(Path(maj.__file__).with_name("cle_publique.txt"))
        self.assertEqual(len(cle), 32)

    def test_http_distant_refuse(self):
        m = maj.Majeur(racine=self.racine, version_locale="3.0.0", cle=PUBLIQUE, autoriser_http_local=True,
                       sources=["http://exemple.invalid/maj/"])
        self.assertFalse(m._url_autorisee("http://exemple.invalid/maj/manifest.json"))
        self.assertTrue(m._url_autorisee("https://exemple.invalid/maj/manifest.json"))
        self.assertFalse(maj.Majeur(racine=self.racine, version_locale="3.0.0", cle=PUBLIQUE)
                         ._url_autorisee(self.github.base + "/x"))


class TestRepli(BaseMaj):
    def test_repli_si_github_en_panne(self):
        self.publier(self.secours, "/maj/")
        self.github.erreurs[CHEMIN_GITHUB + "manifest.json"] = 503
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "disponible")
        self.assertEqual(m.appliquer()["statut"], "pret")
        self.assertEqual(self.relances, [("3.0.0", "3.0.1")])
        self.assertTrue(self.zip_telecharge(self.secours))

    def test_repli_si_github_mal_signe(self):
        self.publier(self.github, CHEMIN_GITHUB, graine=AUTRE_GRAINE)
        self.publier(self.secours, "/maj/")
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "disponible")
        self.assertEqual(m.appliquer()["statut"], "pret")
        self.assertFalse(self.zip_telecharge(self.github))

    def test_repli_du_zip_vers_la_source_du_manifeste(self):
        # Manifeste lu sur le secours, ZIP officiel injoignable : le ZIP est repris à côté du manifeste.
        octets, _, _ = self.publier(self.secours, "/maj/")
        self.secours.erreurs["/objets/AME-Tech-Windows-3.0.1.zip"] = 404
        self.secours.fichiers["/maj/AME-Tech-Windows.zip"] = octets
        self.github.erreurs[CHEMIN_GITHUB + "manifest.json"] = 500
        self.assertEqual(self.majeur().appliquer()["statut"], "pret")

    def test_aucune_source(self):
        m = self.majeur()
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertIn("Aucune source", m.etat()["message"])


class TestApplication(BaseMaj):
    def test_installation_complete(self):
        self.publier(self.github, CHEMIN_GITHUB)
        etat = self.majeur().appliquer()
        self.assertEqual(etat["statut"], "pret", etat["message"])
        dossier = self.racine / "versions" / "3.0.1"
        self.assertEqual((dossier / "ametech" / "version.py").read_text(), 'VERSION = "3.0.1"\n')
        self.assertTrue((dossier / "runtime" / "python" / "pythonw.exe").is_file())
        self.assertEqual((self.racine / "courante.txt").read_text().strip(), "3.0.1")
        self.assertEqual((self.racine / "precedente.txt").read_text().strip(), "3.0.0")
        self.assertEqual(self.relances, [("3.0.0", "3.0.1")])
        self.assertEqual(list((self.racine / "telechargements").iterdir()), [])

    def test_pas_de_seconde_installation_pendant_la_relance(self):
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur()
        self.assertEqual(m.appliquer()["statut"], "pret")
        nb = len(self.github.requetes)
        self.assertEqual(m.appliquer()["statut"], "pret")
        self.assertEqual(m.verifier()["statut"], "pret")
        self.assertEqual(len(self.github.requetes), nb)
        self.assertEqual(len(self.relances), 1)

    def test_empreinte_du_zip_fausse_refusee(self):
        self.publier(self.github, CHEMIN_GITHUB, alterer=lambda m: m["zip"].update(sha256="0" * 64))
        etat = self.majeur().appliquer()
        self.assertEqual(etat["statut"], "erreur")
        self.assertIn("empreinte du ZIP fausse", etat["message"])
        self.assertRienInstalle()

    def test_zip_remplace_sur_le_serveur_refuse(self):
        self.publier(self.github, CHEMIN_GITHUB)
        self.github.fichiers["/objets/AME-Tech-Windows-3.0.1.zip"] += b"octets en trop"
        self.assertEqual(self.majeur().appliquer()["statut"], "erreur")
        self.assertRienInstalle()

    def test_empreinte_d_un_fichier_fausse_refusee(self):
        def alterer(m):
            for f in m["fichiers"]:
                if f["chemin"] == "ametech/__main__.py":
                    f["sha256"] = "1" * 64
        self.publier(self.github, CHEMIN_GITHUB, alterer=alterer)
        etat = self.majeur().appliquer()
        self.assertEqual(etat["statut"], "erreur")
        self.assertIn("Empreinte fausse", etat["message"])
        self.assertRienInstalle()

    def test_fichier_absent_du_manifeste_refuse(self):
        self.publier(self.github, CHEMIN_GITHUB,
                     alterer=lambda m: m.update(fichiers=[f for f in m["fichiers"] if f["chemin"] != "version.txt"]))
        etat = self.majeur().appliquer()
        self.assertIn("absent du manifeste", etat["message"])
        self.assertRienInstalle()

    def test_chemin_dangereux_refuse(self):
        membres = {"ametech/__main__.py": b"", "ametech/version.py": b'VERSION = "3.0.1"\n',
                   "runtime/python/pythonw.exe": b"MZ", "../evade.txt": b"x"}
        url = f"{self.github.base}/objets/AME-Tech-Windows-3.0.1.zip"
        octets, brut, sig = fabriquer_release("3.0.1", url, membres=membres,
                                              alterer=lambda m: m.update(fichiers=[f for f in m["fichiers"]
                                                                                   if ".." not in f["chemin"]]))
        self.github.fichiers.update({"/objets/AME-Tech-Windows-3.0.1.zip": octets,
                                     CHEMIN_GITHUB + "manifest.json": brut, CHEMIN_GITHUB + "manifest.json.sig": sig})
        etat = self.majeur().appliquer()
        self.assertIn("chemin refusé", etat["message"])
        self.assertFalse((Path(self.dossier.name) / "evade.txt").exists())
        self.assertRienInstalle()

    def test_version_du_code_differente_refusee(self):
        membres = {"ametech/__main__.py": b"", "ametech/version.py": b'VERSION = "9.9.9"\n',
                   "runtime/python/pythonw.exe": b"MZ"}
        self.publier(self.github, CHEMIN_GITHUB, membres=membres)
        self.assertIn("ne correspond pas", self.majeur().appliquer()["message"])
        self.assertRienInstalle()

    def test_zip_trop_gros_refuse(self):
        self.publier(self.github, CHEMIN_GITHUB)
        etat = self.majeur(taille_max_zip=100).appliquer()
        self.assertEqual(etat["statut"], "erreur")
        self.assertRienInstalle()

    def test_sans_lanceur_pas_d_application(self):
        self.publier(self.github, CHEMIN_GITHUB)
        m = maj.Majeur(racine=self.racine, version_locale="3.0.0", cle=PUBLIQUE, autoriser_http_local=True,
                       sources=[self.github.base + CHEMIN_GITHUB])
        m.definir_relais(FauxRelais())
        if sys.platform != "win32":
            etat = m.appliquer()
            self.assertEqual(etat["statut"], "disponible")
            self.assertIn("AME Tech.exe", etat["message"])
            self.assertRienInstalle()


class TestRelaisEnLigne(BaseMaj):
    def test_jamais_pendant_une_transmission(self):
        self.publier(self.github, CHEMIN_GITHUB)
        relais = FauxRelais("en_ligne", dernier_envoi=time.time() - 5)
        m = self.majeur(relais=relais)
        etat = m.appliquer()
        self.assertEqual(etat["statut"], "disponible")
        self.assertIn("relais transmet", etat["message"])
        self.assertFalse(self.zip_telecharge(self.github))
        self.assertRienInstalle()

    def test_en_ligne_sans_horodatage_considere_occupe(self):
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur(relais=FauxRelais("en_ligne", None))
        self.assertEqual(m.appliquer()["statut"], "disponible")
        self.assertRienInstalle()

    def test_relais_inconnu_considere_occupe(self):
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur()
        m.definir_relais(None)
        self.assertEqual(m.appliquer()["statut"], "disponible")
        self.assertRienInstalle()

    def test_en_ligne_mais_silencieux_depuis_10_min(self):
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur(relais=FauxRelais("en_ligne", dernier_envoi=time.time() - 601))
        self.assertEqual(m.appliquer()["statut"], "pret")

    def test_occupe_dans_toute_phase_autre_que_arret(self):
        # Un relais qui se reconnecte ou cherche ProPresenter en plein culte reste prioritaire.
        for phase in ("connexion", "recherche_pp", "erreur"):
            for dernier in (time.time() - 5, None):
                m = self.majeur(relais=FauxRelais(phase, dernier_envoi=dernier, phase_depuis=time.time() - 30))
                self.assertTrue(m.relais_occupe(), (phase, dernier))
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur(relais=FauxRelais("connexion", dernier_envoi=time.time() - 5))
        self.assertEqual(m.appliquer()["statut"], "disponible")
        self.assertRienInstalle()

    def test_silencieux_depuis_10_min_quelle_que_soit_la_phase(self):
        for phase in ("connexion", "recherche_pp", "erreur", "en_ligne"):
            m = self.majeur(relais=FauxRelais(phase, dernier_envoi=time.time() - 601))
            self.assertFalse(m.relais_occupe(), phase)
        # Jamais rien transmis : 10 min passées dans la même phase valent 10 min sans texte.
        m = self.majeur(relais=FauxRelais("erreur", dernier_envoi=None, phase_depuis=time.time() - 601))
        self.assertFalse(m.relais_occupe())

    def test_relais_arrete(self):
        self.publier(self.github, CHEMIN_GITHUB)
        self.assertEqual(self.majeur(relais=FauxRelais("arret", time.time())).appliquer()["statut"], "pret")

    def test_boucle_attend_puis_applique(self):
        self.publier(self.github, CHEMIN_GITHUB)
        relais = FauxRelais("en_ligne", dernier_envoi=time.time())
        m = self.majeur(relais=relais)
        arret = threading.Event()
        anciennes = maj.REESSAI_OCCUPE
        maj.REESSAI_OCCUPE = 0.05
        self.addCleanup(setattr, maj, "REESSAI_OCCUPE", anciennes)
        fil = threading.Thread(target=m.boucle, args=(relais, arret, 0.0), daemon=True)
        fil.start()
        time.sleep(0.4)
        self.assertEqual(self.relances, [])
        relais.phase = "arret"
        limite = time.time() + 5
        while not self.relances and time.time() < limite:
            time.sleep(0.05)
        arret.set()
        fil.join(5)
        self.assertEqual(self.relances, [("3.0.0", "3.0.1")])


class TestGarde(BaseMaj):
    def preparer(self):
        for v in ("2.9.0", "3.0.0", "3.0.1"):
            (self.racine / "versions" / v).mkdir(parents=True)
        maj.ecrire_atomique(self.racine / "courante.txt", "3.0.1\n")

    def test_retour_arriere_si_la_nouvelle_version_ne_repond_pas(self):
        self.preparer()
        tues = []
        ok = maj.garder(self.racine, "3.0.0", "3.0.1", delai=0.6, pause=0.05, ports=[self.secours.port],
                        tuer=lambda racine, version: tues.append(version))
        self.assertFalse(ok)
        self.assertEqual((self.racine / "courante.txt").read_text().strip(), "3.0.0")
        self.assertEqual(tues, ["3.0.1"])
        persistant = maj.lire_etat_persistant(self.racine)
        self.assertIn("3.0.1", persistant["echecs"])
        # L'ancienne version redémarrée l'explique et ne retente pas la version fautive.
        self.publier(self.github, CHEMIN_GITHUB)
        m = self.majeur("3.0.0")
        self.assertEqual(m.etat()["statut"], "erreur")
        self.assertIn("3.0.1 n'a pas démarré", m.etat()["message"])
        self.assertEqual(m.verifier()["statut"], "erreur")
        self.assertIn("déjà échoué", m.etat()["message"])
        m.appliquer()
        self.assertEqual(self.relances, [])

    def test_l_ancienne_version_qui_repond_ne_compte_pas(self):
        self.preparer()
        self.secours.fichiers["/api/etat"] = json.dumps({"version": "3.0.0"}).encode()
        ok = maj.garder(self.racine, "3.0.0", "3.0.1", delai=0.5, pause=0.05, ports=[self.secours.port],
                        tuer=lambda *a: None)
        self.assertFalse(ok)
        self.assertEqual((self.racine / "courante.txt").read_text().strip(), "3.0.0")

    def test_nouvelle_version_saine(self):
        self.preparer()
        self.secours.fichiers["/api/etat"] = json.dumps({"version": "3.0.1", "relais": {}}).encode()
        tues = []
        ok = maj.garder(self.racine, "3.0.0", "3.0.1", delai=5, pause=0.05,
                        ports=[self.github.port, self.secours.port], tuer=lambda *a: tues.append(a))
        self.assertTrue(ok)
        self.assertEqual(tues, [])
        self.assertEqual((self.racine / "courante.txt").read_text().strip(), "3.0.1")
        self.assertEqual(sorted(p.name for p in (self.racine / "versions").iterdir()), ["3.0.0", "3.0.1"])

    def test_sonde_reelle(self):
        self.secours.fichiers["/api/etat"] = b'{"version": "3.0.1"}'
        self.assertEqual(maj.sonde_http(self.secours.port), "3.0.1")
        self.assertIsNone(maj.sonde_http(self.github.port))


class TestUtilitaires(unittest.TestCase):
    def test_chemins(self):
        for bon in ("ametech/maj.py", "AME Tech.exe", "runtime/python/python313._pth"):
            self.assertTrue(maj.chemin_sur(bon), bon)
        for mauvais in ("../x", "/abs", "a/../b", "C:/x", "a\\b", "a//b", "con.txt", "x/aux", "fin.", "", "a/./b"):
            self.assertFalse(maj.chemin_sur(mauvais), mauvais)

    def test_versions(self):
        self.assertEqual(maj.cle_version("3.0.1"), (3, 0, 1))
        for mauvaise in ("3.0", "3.0.1.2", "03.0.1", "v3.0.1", "3.0.1 ", None, "3.a.1",
                         "3.0.1\n", "\uff13.0.1", "3.\u0660.1", "3.0.\u0967"):
            self.assertIsNone(maj.cle_version(mauvaise), repr(mauvaise))

    def test_telechargement_qui_goutte_abandonne_dans_le_delai(self):
        """Un serveur qui envoie un octet à la fois ne doit pas tenir le fil au-delà du délai total."""
        import socket
        arret = threading.Event()
        ecoute = socket.socket()
        ecoute.bind(("127.0.0.1", 0))
        ecoute.listen(1)
        port = ecoute.getsockname()[1]

        def goutte():
            conn, _ = ecoute.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
                while not arret.wait(0.05):
                    try:
                        conn.sendall(b"x")
                    except OSError:
                        return

        threading.Thread(target=goutte, daemon=True).start()
        self.addCleanup(ecoute.close)
        self.addCleanup(arret.set)
        with tempfile.TemporaryDirectory() as d:
            m = maj.Majeur(racine=Path(d), version_locale="3.0.0", cle=PUBLIQUE, autoriser_http_local=True,
                           sources=[f"http://127.0.0.1:{port}/"], delai_reseau=2.0)
            resultat = {}

            def lire():
                try:
                    m._lire(f"http://127.0.0.1:{port}/zip", 10_000_000, 0.5, io.BytesIO())
                    resultat["fin"] = "terminé"
                except maj.ErreurMaj as exc:
                    resultat["fin"] = str(exc)

            debut = time.monotonic()
            fil = threading.Thread(target=lire, daemon=True)
            fil.start()
            fil.join(6)
            self.assertFalse(fil.is_alive(), "lecture toujours bloquée après 6 s")
            self.assertIn("trop lent", resultat["fin"])
            self.assertLess(time.monotonic() - debut, 4)

    def test_main_refuse_versions_invalides(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(maj.main(["--garde", "--racine", d, "--ancienne", "..", "--nouvelle", "3.0.1"]), 2)


if __name__ == "__main__":
    unittest.main()
