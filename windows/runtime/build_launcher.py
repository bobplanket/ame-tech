"""Compile « AME Tech.exe » avec Zig (cible x86_64-windows-gnu) et contrôle l'en-tête PE.

Sorties : windows/runtime/.build/AME Tech.exe et .build/BUILD-VERIFICATION.json.
Zig : AMETECH_ZIG, sinon celui extrait par fetch_runtime.py.

Usage : python windows/runtime/build_launcher.py [--version 3.0.0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path

ICI = Path(__file__).resolve().parent
BUILD = ICI / ".build"
SORTIE = BUILD / "AME Tech.exe"
ENTREES = json.loads((ICI / "entrees.json").read_text(encoding="utf-8"))


def version_application() -> str:
    texte = (ICI.parent / "ametech" / "version.py").read_text(encoding="utf-8")
    trouve = re.search(r"""^VERSION\s*=\s*["'](\d+\.\d+\.\d+)["']\s*$""", texte, re.M)
    if not trouve:
        raise SystemExit("VERSION introuvable dans windows/ametech/version.py")
    return trouve.group(1)


def trouver_zig() -> Path:
    if os.environ.get("AMETECH_ZIG"):
        return Path(os.environ["AMETECH_ZIG"])
    version = ENTREES["zig"]["version"]
    for candidat in sorted(BUILD.glob(f"zig-*-{version}")):
        for nom in ("zig", "zig.exe"):
            if (candidat / nom).is_file():
                return candidat / nom
    raise SystemExit("Zig absent : lancez fetch_runtime.py ou définissez AMETECH_ZIG")


def rc_source(version: str) -> str:
    virgules = version.replace(".", ",") + ",0"
    manifeste = (ICI / "launcher.manifest").as_posix()
    return f"""1 24 "{manifeste}"

1 VERSIONINFO
FILEVERSION {virgules}
PRODUCTVERSION {virgules}
FILEOS 0x40004L
FILETYPE 0x1L
FILESUBTYPE 0x0L
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040C04B0"
    BEGIN
      VALUE "CompanyName", "AME Paris"
      VALUE "FileDescription", "AME Tech"
      VALUE "FileVersion", "{version}.0"
      VALUE "InternalName", "AME Tech"
      VALUE "OriginalFilename", "AME Tech.exe"
      VALUE "ProductName", "AME Tech"
      VALUE "ProductVersion", "{version}.0"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x040C, 1200
  END
END
"""


def inspecter_pe(brut: bytes) -> dict:
    if brut[:2] != b"MZ":
        raise SystemExit("Lanceur : signature MZ absente")
    decalage = struct.unpack_from("<I", brut, 0x3C)[0]
    if brut[decalage:decalage + 4] != b"PE\0\0":
        raise SystemExit("Lanceur : en-tête PE absent")
    machine, nb_sections = struct.unpack_from("<HH", brut, decalage + 4)
    taille_opt = struct.unpack_from("<H", brut, decalage + 20)[0]
    opt = decalage + 24
    magic = struct.unpack_from("<H", brut, opt)[0]
    sous_systeme = struct.unpack_from("<H", brut, opt + 68)[0]
    table = opt + taille_opt
    sections = [brut[table + i * 40:table + i * 40 + 8].rstrip(b"\0").decode("ascii", "replace")
                for i in range(nb_sections)]
    return {"machine": machine, "magic": magic, "subsystem": sous_systeme, "sections": sections}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default=None)
    args = parser.parse_args()
    version = args.version or version_application()
    zig = trouver_zig()
    BUILD.mkdir(exist_ok=True)
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(BUILD / "zig-cache"), ZIG_LOCAL_CACHE_DIR=str(BUILD / "zig-cache"))
    (BUILD / "launcher.rc").write_text(rc_source(version), encoding="utf-8")
    subprocess.run([str(zig), "rc", "/fo", str(BUILD / "launcher.res"), str(BUILD / "launcher.rc")],
                   cwd=ICI, env=env, check=True)
    commande = [str(zig), "cc", "-target", "x86_64-windows-gnu", "-std=c11", "-municode",
                "-Wl,--subsystem,windows", "-O2", "-Wall", "-Wextra", "-Werror", "-fno-ident", "-s",
                str(ICI / "launcher.c"), str(BUILD / "launcher.res"), "-o", str(SORTIE),
                "-luser32", "-lkernel32"]
    subprocess.run(commande, cwd=ICI, env=env, check=True)
    brut = SORTIE.read_bytes()
    pe = inspecter_pe(brut)
    assert pe["machine"] == 0x8664, hex(pe["machine"])
    assert pe["magic"] == 0x20B, hex(pe["magic"])
    assert pe["subsystem"] == 2, pe["subsystem"]
    assert ".rsrc" in pe["sections"], pe["sections"]
    assert b'level="asInvoker"' in brut and b"longPathAware" in brut
    resultat = {
        "fichier": SORTIE.name,
        "version": version,
        "octets": len(brut),
        "sha256": hashlib.sha256(brut).hexdigest(),
        "format": "PE32+ AMD64, sous-système Windows GUI (2), manifeste asInvoker",
        "compilateur": "zig " + subprocess.check_output([str(zig), "version"], text=True, env=env).strip(),
        "commande": [c.replace(str(ICI), "<windows/runtime>") for c in commande],
    }
    (BUILD / "BUILD-VERIFICATION.json").write_text(json.dumps(resultat, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")
    print(json.dumps(resultat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
