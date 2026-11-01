"""Lancement au démarrage de Windows : sans effet ailleurs, fichier .cmd de repli sûr."""

import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "windows"))

from ametech import demarrage  # noqa: E402


class TestDemarrage(unittest.TestCase):
    def test_dossier_demarrage(self):
        dossier = demarrage.dossier_demarrage({"APPDATA": "C:/Users/ame/AppData/Roaming"})
        self.assertTrue(str(dossier).replace("\\", "/").endswith("Microsoft/Windows/Start Menu/Programs/Startup"))
        self.assertIsNone(demarrage.dossier_demarrage({}))

    def test_cible_doit_etre_le_lanceur(self):
        self.assertIsNone(demarrage.cible_lanceur({}))
        self.assertIsNone(demarrage.cible_lanceur({"AMETECH_LANCEUR": "C:/x/pythonw.dll"}))
        self.assertIsNotNone(demarrage.cible_lanceur({"AMETECH_LANCEUR": "C:/AME/AME Tech.exe"}))

    def test_contenu_cmd(self):
        cible = PureWindowsPath(r"C:\Users\Église 100%\AME Tech\AME Tech.exe")
        contenu = demarrage.contenu_cmd(cible, "cp850").decode("cp850")
        self.assertEqual(contenu, '@start "" "C:\\Users\\Église 100%%\\AME Tech\\AME Tech.exe"\r\n')
        with self.assertRaises(ValueError):
            demarrage.contenu_cmd(PureWindowsPath('C:\\a"b\\x.exe'))

    @unittest.skipIf(sys.platform == "win32", "comportement hors Windows")
    def test_sans_effet_hors_windows(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"APPDATA": d, "AMETECH_LANCEUR": str(Path(d) / "AME Tech.exe")}
            self.assertFalse(demarrage.activer(True, env))
            self.assertFalse(demarrage.actif(env))
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
