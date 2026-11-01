"""Télécharge les entrées de construction épinglées (entrees.json) sans rien installer.

- Python embarquable officiel 3.13 x64 : vérifié par SHA-256, extrait dans
  windows/runtime/python/ ; seule modification : python313._pth (ajoute la
  racine de l'application, `..\\..`, site désactivé).
- Zig (compilateur du lanceur, construction seulement) pour la plateforme
  courante, extrait dans windows/runtime/.build/.

Usage : python windows/runtime/fetch_runtime.py [--sans-zig] [--cache DOSSIER]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ICI = Path(__file__).resolve().parent
ENTREES = json.loads((ICI / "entrees.json").read_text(encoding="utf-8"))


def empreinte(chemin: Path) -> str:
    with chemin.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def telecharger(url: str, sha256: str, cible: Path) -> Path:
    cible.parent.mkdir(parents=True, exist_ok=True)
    if cible.exists() and empreinte(cible) == sha256:
        print("Entrée en cache vérifiée :", cible.name)
        return cible
    partiel = cible.with_name(cible.name + ".part")
    requete = urllib.request.Request(url, headers={"User-Agent": "AME-Tech-build/3"})
    with urllib.request.urlopen(requete, timeout=120) as reponse, partiel.open("wb") as sortie:
        shutil.copyfileobj(reponse, sortie)
    reel = empreinte(partiel)
    if reel != sha256:
        partiel.unlink(missing_ok=True)
        raise SystemExit(f"Empreinte SHA-256 inattendue pour {cible.name} : {reel}")
    partiel.replace(cible)
    print("Entrée officielle vérifiée :", cible.name, reel)
    return cible


def membres_surs(noms: list[str]) -> None:
    for nom in noms:
        p = PurePosixPath(nom.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            raise SystemExit(f"Membre d'archive dangereux : {nom}")


def extraire_python(archive: Path) -> Path:
    py = ENTREES["python"]
    destination = ICI / "python"
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir()
    with zipfile.ZipFile(archive) as z:
        membres_surs(z.namelist())
        if z.testzip() is not None:
            raise SystemExit("Archive Python corrompue")
        z.extractall(destination)
    (destination / py["pth"]).write_bytes(py["pth_contenu"].encode("ascii"))
    return destination


def plateforme_zig() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine, machine)
    systeme = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    return f"{arch}-{systeme}"


def extraire_zig(archive: Path, cible: str) -> Path:
    racine = ICI / ".build"
    nom = archive.name.removesuffix(".tar.xz").removesuffix(".zip")
    dossier = racine / nom
    if not dossier.is_dir():
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                membres_surs(z.namelist())
                z.extractall(racine)
        else:
            with tarfile.open(archive) as t:
                t.extractall(racine, filter="data")
    executable = dossier / ("zig.exe" if cible.endswith("windows") else "zig")
    if not executable.is_file():
        raise SystemExit(f"Zig introuvable après extraction : {executable}")
    return executable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sans-zig", action="store_true", help="ne pas télécharger le compilateur")
    parser.add_argument("--cache", type=Path, default=ICI / ".build" / "sources")
    args = parser.parse_args()
    py = ENTREES["python"]
    archive = telecharger(py["url"], py["sha256"], args.cache / py["fichier"])
    print("Moteur Python extrait dans", extraire_python(archive))
    if not args.sans_zig:
        cible = plateforme_zig()
        zig = ENTREES["zig"]["plateformes"].get(cible)
        if zig is None:
            raise SystemExit(f"Pas de Zig épinglé pour {cible} (voir entrees.json)")
        archive_zig = telecharger(zig["url"], zig["sha256"], args.cache / zig["url"].rsplit("/", 1)[1])
        print("Compilateur disponible :", extraire_zig(archive_zig, cible))


if __name__ == "__main__":
    main()
