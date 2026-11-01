"""Chaîne de publication : génération de clés, ZIP + manifeste, signature, puis
application par le client depuis un faux GitHub local (bout en bout)."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

DEPOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPOT / "windows"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ametech import ed25519, maj  # noqa: E402
from test_maj import FauxRelais, Serveur  # noqa: E402


def charger(nom, chemin):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_zip = charger("ametech_build_zip", DEPOT / "windows" / "build_zip.py")
signer_manifeste = charger("ametech_signer", DEPOT / "outils" / "signer_manifeste.py")
generer_cles = charger("ametech_generer", DEPOT / "outils" / "generer_cles.py")

GRAINE = hashlib.sha256(b"graine des tests de publication").digest()
PUBLIQUE = ed25519.cle_publique(GRAINE)


class TestGenererCles(unittest.TestCase):
    def test_paire_generee_et_graine_sur_stdout_seulement(self):
        with tempfile.TemporaryDirectory() as d:
            fichier = Path(d) / "cle_publique.txt"
            fichier.write_text("# CLE-DE-TEST\nAAAA\n")
            sortie, erreur = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreur):
                self.assertEqual(generer_cles.main(["--cle-publique", str(fichier)]), 0)
            graine = base64.b64decode(sortie.getvalue().strip(), validate=True)
            self.assertEqual(len(graine), 32)
            self.assertNotIn(sortie.getvalue().strip(), erreur.getvalue())
            self.assertNotIn(sortie.getvalue().strip(), fichier.read_text())
            cle, est_test = maj.charger_cle(fichier)
            self.assertFalse(est_test)
            self.assertEqual(cle, ed25519.cle_publique(graine))
            # Une vraie clé n'est pas écrasée sans --remplacer.
            with contextlib.redirect_stdout(io.StringIO()) as s2, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generer_cles.main(["--cle-publique", str(fichier)]), 3)
            self.assertEqual(s2.getvalue(), "")
            self.assertEqual(maj.charger_cle(fichier)[0], cle)


class TestSigner(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.dossier = Path(self.d.name)
        self.manifeste = self.dossier / "manifest.json"
        self.manifeste.write_bytes(b'{"version": "3.0.1", "zip": {}}\n')
        self.cle = self.dossier / "cle.txt"
        self.cle.write_text(base64.b64encode(PUBLIQUE).decode() + "\n")

    def signer(self, graine, *options):
        env = {"AMETECH_SIGNING_KEY": base64.b64encode(graine).decode() if graine else ""}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as sortie, \
                contextlib.redirect_stderr(io.StringIO()) as erreur:
            code = signer_manifeste.main([str(self.manifeste), "--cle-publique", str(self.cle), *options])
        secret = env["AMETECH_SIGNING_KEY"]
        if secret:
            self.assertNotIn(secret, sortie.getvalue() + erreur.getvalue())
        return code

    def test_signature_valide(self):
        self.assertEqual(self.signer(GRAINE), 0)
        sig = base64.b64decode((self.dossier / "manifest.json.sig").read_text().strip())
        self.assertTrue(ed25519.verifier(PUBLIQUE, self.manifeste.read_bytes(), sig))

    def test_graine_qui_ne_correspond_pas(self):
        self.assertEqual(self.signer(hashlib.sha256(b"autre").digest()), 3)
        self.assertFalse((self.dossier / "manifest.json.sig").exists())

    def test_cle_de_test_refusee_sans_option(self):
        self.cle.write_text("# CLE-DE-TEST\n" + base64.b64encode(PUBLIQUE).decode() + "\n")
        self.assertEqual(self.signer(GRAINE), 3)
        self.assertEqual(self.signer(GRAINE, "--autoriser-cle-test"), 0)

    def test_secret_absent(self):
        self.assertEqual(self.signer(b""), 2)

    def test_cle_test_du_depot_refusee(self):
        # La clé livrée dans le dépôt est marquée test : une release réelle échouerait.
        _, est_test = maj.charger_cle(DEPOT / "windows" / "ametech" / "cle_publique.txt")
        if est_test:
            with mock.patch.dict(os.environ, {"AMETECH_SIGNING_KEY": base64.b64encode(GRAINE).decode()}), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(signer_manifeste.main([str(self.manifeste)]), 3)


class TestConstructionBoutEnBout(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        base = Path(self.d.name)
        self.appli = base / "ametech"
        (self.appli / "www").mkdir(parents=True)
        (self.appli / "__pycache__").mkdir()
        (self.appli / "tests").mkdir()
        for nom in ("maj.py", "ed25519.py", "demarrage.py"):
            (self.appli / nom).write_bytes((DEPOT / "windows" / "ametech" / nom).read_bytes())
        (self.appli / "__init__.py").write_text("")
        (self.appli / "__main__.py").write_text("print('AME Tech')\n")
        (self.appli / "version.py").write_text('VERSION = "3.0.1"\n')
        (self.appli / "cle_publique.txt").write_text(base64.b64encode(PUBLIQUE).decode() + "\n")
        (self.appli / "www" / "index.html").write_text("<!doctype html>")
        (self.appli / "__pycache__" / "maj.cpython-313.pyc").write_bytes(b"cache")
        (self.appli / "tests" / "test_x.py").write_text("")
        self.python = base / "python"
        self.python.mkdir()
        for nom in ("pythonw.exe", "python.exe", "python313.dll", "python313._pth"):
            (self.python / nom).write_bytes(b"MZ " + nom.encode())
        self.lanceur = base / "lanceur" / "AME Tech.exe"
        self.lanceur.parent.mkdir()
        self.lanceur.write_bytes(b"MZ faux lanceur")
        self.lua = base / "ame-tech.lua"
        self.lua.write_text("-- script OBS\n")
        self.sortie = base / "dist"
        self.serveur = Serveur()
        self.addCleanup(self.serveur.fermer)

    def construire(self, *options):
        with contextlib.redirect_stdout(io.StringIO()):
            return build_zip.main(["--appli", str(self.appli), "--python", str(self.python),
                                   "--lanceur", str(self.lanceur), "--sortie", str(self.sortie),
                                   "--sans-controle-runtime", "--supplement", str(self.lua),
                                   "--base-url", self.serveur.base + "/rel", "--notes", "Essai", *options])

    def test_tag_qui_ne_correspond_pas(self):
        with self.assertRaises(SystemExit):
            self.construire("--tag", "v3.0.2")

    def test_zip_manifeste_signature_puis_application(self):
        self.construire("--tag", "v3.0.1")
        brut = (self.sortie / "manifest.json").read_bytes()
        manifeste = json.loads(brut)
        self.assertEqual(manifeste["version"], "3.0.1")
        with zipfile.ZipFile(self.sortie / "AME-Tech-Windows.zip") as z:
            noms = set(z.namelist())
            lire_moi = z.read("LIRE-MOI.txt").decode("utf-8-sig")
        self.assertIn("AME Tech.exe", noms)
        self.assertIn("ametech/www/index.html", noms)
        self.assertIn("runtime/python/pythonw.exe", noms)
        self.assertFalse(any("__pycache__" in n or "/tests/" in n or n.endswith(".pyc") for n in noms))
        self.assertIn("AME Tech 3.0.1", lire_moi)
        self.assertNotIn("\u2014", lire_moi)
        self.assertNotIn("\u2013", lire_moi)
        par_chemin = {f["chemin"]: f for f in manifeste["fichiers"]}
        self.assertEqual(par_chemin["ame-tech.lua"]["url"], self.serveur.base + "/rel/ame-tech.lua")
        self.assertEqual(set(par_chemin) - {"ame-tech.lua"}, noms)
        self.assertEqual(manifeste["zip"]["sha256"],
                         hashlib.sha256((self.sortie / "AME-Tech-Windows.zip").read_bytes()).hexdigest())

        cle = Path(self.d.name) / "cle.txt"
        cle.write_text(base64.b64encode(PUBLIQUE).decode() + "\n")
        with mock.patch.dict(os.environ, {"AMETECH_SIGNING_KEY": base64.b64encode(GRAINE).decode()}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(signer_manifeste.main([str(self.sortie / "manifest.json"), "--cle-publique", str(cle)]), 0)

        for nom in ("AME-Tech-Windows.zip", "manifest.json", "manifest.json.sig", "ame-tech.lua"):
            self.serveur.fichiers["/rel/" + nom] = (self.sortie / nom).read_bytes()
        relances = []
        racine = Path(self.d.name) / "donnees"
        client = maj.Majeur(racine=racine, version_locale="3.0.0", cle=PUBLIQUE, autoriser_http_local=True,
                            sources=[self.serveur.base + "/rel/"], relancer=lambda a, n: relances.append((a, n)))
        client.definir_relais(FauxRelais("arret"))
        etat = client.appliquer()
        self.assertEqual(etat["statut"], "pret", etat["message"])
        self.assertEqual(relances, [("3.0.0", "3.0.1")])
        self.assertTrue((racine / "versions" / "3.0.1" / "AME Tech.exe").is_file())
        self.assertEqual((racine / "versions" / "3.0.1" / "version.txt").read_text(), "3.0.1\n")


if __name__ == "__main__":
    unittest.main()
