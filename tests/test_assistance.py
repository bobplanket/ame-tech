"""Tests du module d'assistance RustDesk (stdlib, exécutables sur Mac).

Lancement : python3 -m unittest discover -s tests -p 'test_assistance.py'
"""
from __future__ import annotations

import hashlib
import http.server
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "windows"))

from ametech import assistance  # noqa: E402

CONTENU = b"MZ faux executable RustDesk " * 4000
SHA_CONTENU = hashlib.sha256(CONTENU).hexdigest()


class _Gestionnaire(http.server.BaseHTTPRequestHandler):
    corps = CONTENU

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.corps)))
        self.end_headers()
        self.wfile.write(self.corps)

    def log_message(self, *args):
        pass


def _analyse_rustdesk(nom: str) -> dict:
    """Reprise de get_custom_server_from_string (rustdesk 1.5.0, custom_server.rs)."""
    s = nom[:-4] if nom.lower().endswith(".exe") else nom
    assert "host=" in s.lower()
    s = s[s.lower().find("host="):]
    res = {"host": "", "key": ""}
    for el in s.split(","):
        if el.lower().startswith("host="):
            res["host"] = el[5:]
        if el.lower().startswith("key="):
            res["key"] = el[4:]
    return res


class BaseTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name,
                                           "AMETECH_DOSSIER": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def serveur(self, corps: bytes = CONTENU) -> str:
        gestionnaire = type("G", (_Gestionnaire,), {"corps": corps})
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gestionnaire)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}/rustdesk.exe"


class TestConfiguration(unittest.TestCase):
    def test_nom_executable_porte_hote_et_cle(self):
        nom = assistance.nom_executable()
        self.assertEqual(nom, f"rustdesk-host=tech.ame.paris,key={assistance.CLE_PUBLIQUE},.exe")
        lu = _analyse_rustdesk(nom)
        self.assertEqual(lu["host"], "tech.ame.paris")
        self.assertEqual(lu["key"], assistance.CLE_PUBLIQUE)

    def test_nom_resiste_au_suffixe_windows(self):
        nom = assistance.nom_executable().replace(",.exe", ", (1).exe")
        self.assertEqual(_analyse_rustdesk(nom)["key"], assistance.CLE_PUBLIQUE)

    def test_cle_en_dur_valide_pour_windows(self):
        self.assertEqual(len(assistance.CLE_PUBLIQUE), 44)
        for c in '<>:"/\\|?*,':
            self.assertNotIn(c, assistance.CLE_PUBLIQUE)

    def test_cle_avec_barre_refusee(self):
        with self.assertRaises(ValueError):
            assistance.nom_executable(cle="abc/def=")
        with self.assertRaises(ValueError):
            assistance.nom_executable(hote="")

    def test_chemin_dans_localappdata(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LOCALAPPDATA": d}):
            self.assertEqual(assistance.chemin_executable(),
                             Path(d) / "AME Tech" / "rustdesk" / assistance.nom_executable())

    def test_constantes_du_contrat(self):
        self.assertEqual(assistance.CONTACT, {
            "nom": "Will Hardy", "whatsapp": "+1 (438) 458-2683",
            "wa_me": "https://wa.me/14384582683", "courriel": "bonjour@tmgconseil.fr"})
        self.assertEqual(assistance.TEAMVIEWER_URL,
                         "https://www.teamviewer.com/fr/telechargement/windows/")
        self.assertRegex(assistance.SHA256_RUSTDESK, r"^[0-9a-f]{64}$")
        self.assertIn(f"/download/{assistance.VERSION_RUSTDESK}/", assistance.URL_RUSTDESK)
        self.assertTrue(assistance.URL_RUSTDESK.startswith(
            "https://github.com/rustdesk/rustdesk/releases/download/"))

    def test_pas_de_tiret_long(self):
        source = Path(assistance.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\u2014", source)
        self.assertNotIn("\u2013", source)

    def test_extraire_id_sortie_paquet_portable(self):
        sortie = ("is windows7: false\nskip rustdesk.exe\nskip flutter_windows.dll\n"
                  "executing C:\\Users\\a\\AppData\\Local\\rustdesk\\rustdesk.exe\n123456789\n")
        self.assertEqual(assistance.extraire_id(sortie), "123456789")
        self.assertIsNone(assistance.extraire_id("skip rustdesk.exe\n\n"))


class TestTelechargement(BaseTemp):
    def test_empreinte_correcte_acceptee(self):
        dest = Path(self.tmp.name) / "x" / "r.exe"
        assistance.telecharger(self.serveur(), dest, SHA_CONTENU, len(CONTENU))
        self.assertEqual(dest.read_bytes(), CONTENU)
        self.assertFalse(dest.with_name("r.exe.part").exists())

    def test_empreinte_fausse_refusee(self):
        dest = Path(self.tmp.name) / "r.exe"
        with self.assertRaises(assistance.ErreurAssistance) as ctx:
            assistance.telecharger(self.serveur(), dest, "0" * 64, len(CONTENU))
        self.assertIn("empreinte", str(ctx.exception))
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("r.exe.part").exists())

    def test_fichier_trop_gros_refuse(self):
        dest = Path(self.tmp.name) / "r.exe"
        with self.assertRaises(assistance.ErreurAssistance):
            assistance.telecharger(self.serveur(CONTENU + b"x"), dest, SHA_CONTENU, len(CONTENU))
        self.assertFalse(dest.exists())

    def test_serveur_injoignable(self):
        dest = Path(self.tmp.name) / "r.exe"
        with self.assertRaises(assistance.ErreurAssistance):
            assistance.telecharger("http://127.0.0.1:9/absent", dest, SHA_CONTENU)
        self.assertFalse(dest.exists())


class TestEtatEtLancement(BaseTemp):
    def setUp(self):
        super().setUp()
        self.url = self.serveur()
        for nom, valeur in (("URL_RUSTDESK", self.url), ("SHA256_RUSTDESK", SHA_CONTENU),
                            ("TAILLE_RUSTDESK", len(CONTENU)), ("_EST_WINDOWS", True),
                            ("_rustdesk_en_cours", lambda: False)):
            p = mock.patch.object(assistance, nom, valeur)
            p.start()
            self.addCleanup(p.stop)

    def test_etat_sans_rustdesk(self):
        self.assertEqual(assistance.etat(),
                         {"rustdesk": "absent", "id": None, "serveur": "tech.ame.paris"})

    def test_lancement_complet(self):
        lances = []
        with mock.patch.object(assistance, "_lancer_processus", lances.append), \
                mock.patch.object(assistance, "_lire_id", lambda c: "987654321"):
            res = assistance.lancer_rustdesk()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "987654321")
        self.assertIn("987654321", res["message"])
        attendu = Path(self.tmp.name) / "AME Tech" / "rustdesk" / assistance.nom_executable()
        self.assertEqual(lances, [attendu])
        self.assertEqual(attendu.read_bytes(), CONTENU)
        self.assertEqual(assistance.etat(),
                         {"rustdesk": "pret", "id": "987654321", "serveur": "tech.ame.paris"})
        with mock.patch.object(assistance, "_rustdesk_en_cours", lambda: True):
            self.assertEqual(assistance.etat()["rustdesk"], "lance")

    def test_pas_de_second_telechargement(self):
        with mock.patch.object(assistance, "_lancer_processus", lambda c: None), \
                mock.patch.object(assistance, "_lire_id", lambda c: "111222333"):
            self.assertTrue(assistance.lancer_rustdesk()["ok"])
            with mock.patch.object(assistance, "telecharger",
                                   side_effect=AssertionError("re-téléchargement")):
                self.assertTrue(assistance.lancer_rustdesk()["ok"])

    def test_executable_altere_retelecharge(self):
        chemin = assistance.chemin_executable()
        chemin.parent.mkdir(parents=True)
        chemin.write_bytes(b"Z" * len(CONTENU))  # bonne taille, mauvais contenu
        lances = []
        with mock.patch.object(assistance, "_lancer_processus", lances.append), \
                mock.patch.object(assistance, "_lire_id", lambda c: "123123123"):
            self.assertTrue(assistance.lancer_rustdesk()["ok"])
        self.assertEqual(chemin.read_bytes(), CONTENU)

    def test_lancement_empreinte_fausse(self):
        lances = []
        with mock.patch.object(assistance, "SHA256_RUSTDESK", "f" * 64), \
                mock.patch.object(assistance, "_lancer_processus", lances.append):
            res = assistance.lancer_rustdesk()
        self.assertFalse(res["ok"])
        self.assertIn("empreinte", res["message"])
        self.assertEqual(lances, [])
        self.assertEqual(res["rustdesk"], "absent")
        self.assertFalse(assistance.chemin_executable().exists())

    def test_identifiant_indisponible(self):
        with mock.patch.object(assistance, "_lancer_processus", lambda c: None), \
                mock.patch.object(assistance, "_lire_id", lambda c: None), \
                mock.patch.object(assistance, "DELAI_ID_S", 0):
            res = assistance.lancer_rustdesk()
        self.assertTrue(res["ok"])
        self.assertIsNone(res["id"])
        self.assertIn("pas pu être lu", res["message"])

    def test_hors_windows(self):
        with mock.patch.object(assistance, "_EST_WINDOWS", False):
            res = assistance.lancer_rustdesk()
        self.assertFalse(res["ok"])
        self.assertIn("Windows", res["message"])


if __name__ == "__main__":
    unittest.main()
