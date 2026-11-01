"""Construit AME-Tech-Windows.zip et manifest.json (format de docs/CONTRAT.md).

Entrées :
- windows/runtime/.build/AME Tech.exe  (build_launcher.py)
- windows/runtime/python/              (fetch_runtime.py, contrôlé contre l'archive officielle)
- windows/ametech/                     (tout le paquet, www/ compris, sans tests ni caches)

Contenu du ZIP (identique à un dossier %LOCALAPPDATA%\\AME Tech\\versions\\<v>\\) :
    AME Tech.exe, LIRE-MOI.txt, version.txt, ametech/..., runtime/python/...,
    runtime/PROVENANCE.json, runtime/LICENSE-MinGW.txt, runtime/launcher.c

Le manifeste liste chaque membre du ZIP (chemin, sha256, url = URL du ZIP) et
chaque fichier publié à part (--supplement, url propre). Le ZIP produit est
ensuite rejoué par ametech.maj (analyse du manifeste + extraction contrôlée)
pour prouver que le client l'acceptera. La signature se fait à part
(outils/signer_manifeste.py), seulement en CI.

Usage : python windows/build_zip.py [--tag v3.0.1] [--sortie dist] [--supplement obs/ame-tech.lua]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

WINDOWS = Path(__file__).resolve().parent
RUNTIME = WINDOWS / "runtime"
APPLI = WINDOWS / "ametech"
DEPOT = "bobplanket/ame-tech"
NOM_ZIP = "AME-Tech-Windows.zip"
EXCLUS_DOSSIERS = {"__pycache__", "tests", ".pytest_cache"}
EXCLUS_SUFFIXES = (".pyc", ".pyo")


def version_application(appli: Path = APPLI) -> str:
    texte = (appli / "version.py").read_text(encoding="utf-8")
    trouve = re.search(r"""^VERSION\s*=\s*["'](\d+\.\d+\.\d+)["']\s*$""", texte, re.M)
    if not trouve:
        raise SystemExit("VERSION introuvable dans windows/ametech/version.py")
    return trouve.group(1)


def sha256(donnees: bytes) -> str:
    return hashlib.sha256(donnees).hexdigest()


def fichiers_appli(appli: Path = APPLI) -> dict[str, bytes]:
    contenu = {}
    for chemin in sorted(appli.rglob("*")):
        relatif = chemin.relative_to(appli)
        if not chemin.is_file() or any(p in EXCLUS_DOSSIERS or p.startswith(".") for p in relatif.parts):
            continue
        if chemin.suffix in EXCLUS_SUFFIXES:
            continue
        contenu["ametech/" + relatif.as_posix()] = chemin.read_bytes()
    for requis in ("ametech/__main__.py", "ametech/version.py", "ametech/maj.py", "ametech/cle_publique.txt",
                   "ametech/www/index.html"):
        if requis not in contenu:
            raise SystemExit(f"Fichier requis absent du paquet : {requis}")
    return contenu


def fichiers_runtime(dossier: Path, controler: bool, archive: Path | None) -> dict[str, bytes]:
    entrees = json.loads((RUNTIME / "entrees.json").read_text(encoding="utf-8"))["python"]
    contenu = {"runtime/python/" + p.relative_to(dossier).as_posix(): p.read_bytes()
               for p in sorted(dossier.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    if "runtime/python/pythonw.exe" not in contenu:
        raise SystemExit("Moteur Python absent : lancez windows/runtime/fetch_runtime.py")
    if controler:
        archive = archive or RUNTIME / ".build" / "sources" / entrees["fichier"]
        if sha256(archive.read_bytes()) != entrees["sha256"]:
            raise SystemExit("Archive Python officielle : empreinte inattendue")
        with zipfile.ZipFile(archive) as officiel:
            attendus = {}
            for nom in officiel.namelist():
                if nom.endswith("/"):
                    continue
                donnees = entrees["pth_contenu"].encode("ascii") if nom == entrees["pth"] else officiel.read(nom)
                attendus["runtime/python/" + nom] = donnees
        if attendus != contenu:
            ecarts = sorted(set(attendus) ^ set(contenu)) or [n for n in attendus if attendus[n] != contenu[n]]
            raise SystemExit(f"Moteur Python modifié par rapport à l'archive officielle : {ecarts[:5]}")
    return contenu


def horodatage() -> dt.datetime:
    source = os.environ.get("SOURCE_DATE_EPOCH")
    if source and source.isdigit():
        return dt.datetime.fromtimestamp(int(source), dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ecrire_zip(chemin: Path, contenu: dict[str, bytes], quand: dt.datetime) -> None:
    date_zip = max(quand, dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc)).timetuple()[:6]
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for nom in sorted(contenu):
            info = zipfile.ZipInfo(nom, date_time=date_zip)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 << 16)
            info.create_system = 3
            z.writestr(info, contenu[nom])


def charger_maj():
    sys.path.insert(0, str(WINDOWS))
    try:
        from ametech import maj
    finally:
        sys.path.remove(str(WINDOWS))
    return maj


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default=None)
    parser.add_argument("--tag", default=None, help="tag Git, doit valoir v<VERSION>")
    parser.add_argument("--sortie", type=Path, default=RUNTIME / "dist")
    parser.add_argument("--base-url", default=None, help="défaut : release GitHub du tag")
    parser.add_argument("--notes", default="")
    parser.add_argument("--lanceur", type=Path, default=RUNTIME / ".build" / "AME Tech.exe")
    parser.add_argument("--python", type=Path, default=RUNTIME / "python")
    parser.add_argument("--appli", type=Path, default=APPLI, help="dossier du paquet ametech")
    parser.add_argument("--archive-python", type=Path, default=None)
    parser.add_argument("--sans-controle-runtime", action="store_true", help="tests seulement")
    parser.add_argument("--supplement", type=Path, action="append", default=[],
                        help="fichier publié à côté du ZIP et listé dans le manifeste")
    args = parser.parse_args(argv)

    version = args.version or version_application(args.appli)
    if version != version_application(args.appli):
        raise SystemExit(f"--version {version} différente de version.py ({version_application(args.appli)})")
    if args.tag is not None and args.tag != f"v{version}":
        raise SystemExit(f"Le tag {args.tag} ne correspond pas à la version {version} (attendu v{version})")
    base = (args.base_url or f"https://github.com/{DEPOT}/releases/download/v{version}").rstrip("/")
    url_zip = f"{base}/{NOM_ZIP}"

    lanceur = args.lanceur.read_bytes()
    verification = args.lanceur.with_name("BUILD-VERIFICATION.json")
    construction = json.loads(verification.read_text(encoding="utf-8")) if verification.is_file() else None
    if construction is not None and construction.get("sha256") != sha256(lanceur):
        raise SystemExit("Le lanceur ne correspond pas à BUILD-VERIFICATION.json")
    if lanceur[:2] != b"MZ":
        raise SystemExit("Le lanceur n'est pas un exécutable Windows")

    lire_moi = (RUNTIME / "LIRE-MOI.txt").read_text(encoding="utf-8").replace("{version}", version)
    if any(c in lire_moi for c in "\u2014\u2013") or "AMETECH1:" in lire_moi or "otpauth" in lire_moi:
        raise SystemExit("LIRE-MOI.txt : tiret long ou code d'accès interdit")
    entrees = json.loads((RUNTIME / "entrees.json").read_text(encoding="utf-8"))
    provenance = {"python": entrees["python"], "zig": {k: entrees["zig"][k] for k in ("version", "role", "source_empreinte")},
                  "lanceur": construction}

    contenu: dict[str, bytes] = {
        "AME Tech.exe": lanceur,
        "LIRE-MOI.txt": lire_moi.replace("\n", "\r\n").encode("utf-8-sig"),
        "version.txt": f"{version}\n".encode("ascii"),
        "runtime/PROVENANCE.json": (json.dumps(provenance, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "runtime/LICENSE-MinGW.txt": (RUNTIME / "LICENSE-MinGW.txt").read_bytes(),
        "runtime/launcher.c": (RUNTIME / "launcher.c").read_bytes(),
    }
    contenu.update(fichiers_appli(args.appli))
    contenu.update(fichiers_runtime(args.python, not args.sans_controle_runtime, args.archive_python))

    args.sortie.mkdir(parents=True, exist_ok=True)
    quand = horodatage()
    chemin_zip = args.sortie / NOM_ZIP
    temporaire = chemin_zip.with_name(NOM_ZIP + ".tmp")
    ecrire_zip(temporaire, contenu, quand)
    octets_zip = temporaire.read_bytes()

    fichiers = [{"chemin": nom, "sha256": sha256(donnees), "url": url_zip} for nom, donnees in sorted(contenu.items())]
    for supplement in args.supplement:
        donnees = supplement.read_bytes()
        fichiers.append({"chemin": supplement.name, "sha256": sha256(donnees), "url": f"{base}/{supplement.name}"})
        shutil.copyfile(supplement, args.sortie / supplement.name)
    manifeste = {
        "version": version,
        "date": quand.isoformat().replace("+00:00", "Z"),
        "notes": args.notes,
        "fichiers": fichiers,
        "zip": {"url": url_zip, "sha256": sha256(octets_zip), "taille": len(octets_zip)},
    }
    brut = (json.dumps(manifeste, ensure_ascii=False, indent=1) + "\n").encode("utf-8")

    # Rejeu par le client : ce qui sort d'ici doit passer les contrôles de maj.py.
    maj = charger_maj()
    relu = maj.analyser_manifeste(brut)
    with tempfile.TemporaryDirectory() as dossier:
        membres = maj.extraire_zip(temporaire, Path(dossier) / version, relu)
    if membres != set(contenu):
        raise SystemExit("Rejeu : membres du ZIP inattendus")

    temporaire.replace(chemin_zip)
    (args.sortie / "manifest.json").write_bytes(brut)
    resume = {"version": version, "zip": str(chemin_zip), "octets": len(octets_zip),
              "sha256": manifeste["zip"]["sha256"], "fichiers": len(fichiers)}
    print(json.dumps(resume, ensure_ascii=False, indent=2))
    return resume


if __name__ == "__main__":
    main()
