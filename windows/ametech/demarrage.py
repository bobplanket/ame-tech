"""Lancement d'AME Tech à l'ouverture de session Windows.

Dépose un raccourci « AME Tech.lnk » dans le dossier Démarrage de
l'utilisateur (%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup) :
aucun droit administrateur, aucune clé de registre. Le raccourci est écrit par
l'objet COM ShellLink de Windows via ctypes (bibliothèque standard). Si COM
échoue, repli sur un « AME Tech.cmd » d'une ligne (bref passage d'une console).

La cible est le lanceur `AME Tech.exe` en cours (variable AMETECH_LANCEUR posée
par le lanceur). Hors Windows, toutes les fonctions sont sans effet.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

NOM = "AME Tech"


def dossier_demarrage(env=None) -> Path | None:
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def cible_lanceur(env=None) -> Path | None:
    env = os.environ if env is None else env
    valeur = env.get("AMETECH_LANCEUR")
    if not valeur or not valeur.lower().endswith(".exe"):
        return None
    return Path(valeur)


def contenu_cmd(cible: Path, page_de_code: str = "cp850") -> bytes:
    """Fichier .cmd minimal qui lance `cible` sans attendre. Lève ValueError si le chemin est inutilisable."""
    texte = str(cible)
    if '"' in texte or any(ord(c) < 32 for c in texte):
        raise ValueError("Chemin du lanceur inutilisable dans un fichier .cmd.")
    texte = texte.replace("%", "%%")
    return f'@start "" "{texte}"\r\n'.encode(page_de_code)


def actif(env=None) -> bool:
    if sys.platform != "win32":
        return False
    dossier = dossier_demarrage(env)
    return bool(dossier) and ((dossier / f"{NOM}.lnk").is_file() or (dossier / f"{NOM}.cmd").is_file())


def activer(valeur: bool, env=None) -> bool:
    """Active ou retire le lancement au démarrage. Renvoie l'état obtenu (actif())."""
    if sys.platform != "win32":
        return False
    dossier = dossier_demarrage(env)
    if dossier is None:
        return False
    lnk, cmd = dossier / f"{NOM}.lnk", dossier / f"{NOM}.cmd"
    if not valeur:
        for fichier in (lnk, cmd):
            try:
                fichier.unlink(missing_ok=True)
            except OSError:
                pass
        return actif(env)
    cible = cible_lanceur(env)
    if cible is None or not cible.is_file():
        return actif(env)
    dossier.mkdir(parents=True, exist_ok=True)
    if _creer_lnk(lnk, cible):
        try:
            cmd.unlink(missing_ok=True)
        except OSError:
            pass
        return actif(env)
    try:
        import ctypes
        page = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        temporaire = cmd.with_suffix(".cmd.tmp")
        temporaire.write_bytes(contenu_cmd(cible, page))
        os.replace(temporaire, cmd)
    except (OSError, ValueError, LookupError, UnicodeEncodeError):
        pass
    return actif(env)


def _creer_lnk(chemin: Path, cible: Path) -> bool:
    """Écrit le raccourci dans un fil dédié (appartement COM propre)."""
    resultat = []
    fil = threading.Thread(target=lambda: resultat.append(_creer_lnk_com(chemin, cible)), daemon=True)
    fil.start()
    fil.join(15)
    return bool(resultat and resultat[0])


def _creer_lnk_com(chemin: Path, cible: Path) -> bool:
    import ctypes
    import uuid
    from ctypes import POINTER, byref, c_void_p, c_wchar_p, wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("donnees", ctypes.c_ubyte * 16)]

    def guid(texte: str) -> GUID:
        g = GUID()
        ctypes.memmove(byref(g), uuid.UUID(texte).bytes_le, 16)
        return g

    def methode(objet: c_void_p, index: int, *types):
        vtable = ctypes.cast(objet, POINTER(POINTER(c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *types)
        fonction = prototype(vtable[index])
        return lambda *args: fonction(objet, *args)

    def liberer(objet: c_void_p) -> None:
        if objet:
            vtable = ctypes.cast(objet, POINTER(POINTER(c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(objet)

    ole32 = ctypes.OleDLL("ole32")
    initialise = False
    lien = c_void_p()
    fichier = c_void_p()
    try:
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        initialise = True
        clsid = guid("00021401-0000-0000-C000-000000000046")    # CLSID_ShellLink
        iid_lien = guid("000214F9-0000-0000-C000-000000000046")  # IID_IShellLinkW
        iid_fichier = guid("0000010b-0000-0000-C000-000000000046")  # IID_IPersistFile
        ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid_lien), byref(lien))  # CLSCTX_INPROC_SERVER
        methode(lien, 20, c_wchar_p)(str(cible))                 # SetPath
        methode(lien, 9, c_wchar_p)(str(cible.parent))           # SetWorkingDirectory
        methode(lien, 7, c_wchar_p)("AME Tech : relais ProPresenter")  # SetDescription
        methode(lien, 17, c_wchar_p, ctypes.c_int)(str(cible), 0)      # SetIconLocation
        methode(lien, 0, POINTER(GUID), POINTER(c_void_p))(byref(iid_fichier), byref(fichier))  # QueryInterface
        temporaire = chemin.with_name(chemin.stem + ".tmp.lnk")
        methode(fichier, 6, c_wchar_p, wintypes.BOOL)(str(temporaire), True)  # IPersistFile::Save
        os.replace(temporaire, chemin)
        return True
    except OSError:
        return False
    finally:
        liberer(fichier)
        liberer(lien)
        if initialise:
            ctypes.windll.ole32.CoUninitialize()  # void : pas de contrôle HRESULT
