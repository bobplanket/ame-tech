"""Code d'accès à 6 chiffres (TOTP, RFC 6238 : SHA-1, pas de 30 s).

Le même calcul tourne hors ligne dans le logiciel Windows et l'appli Mac ;
le serveur accepte le pas précédent et le suivant pour absorber un léger décalage d'horloge.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import parse_qs, urlsplit

PAS = 30
CHIFFRES = 6
PREFIXE = "AMETECH1:"


def nouveau_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _cle(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def _hotp(cle: bytes, compteur: int) -> str:
    mac = hmac.new(cle, struct.pack(">Q", compteur), hashlib.sha1).digest()
    decalage = mac[-1] & 0x0F
    valeur = struct.unpack(">I", mac[decalage:decalage + 4])[0] & 0x7FFFFFFF
    return str(valeur % 10 ** CHIFFRES).zfill(CHIFFRES)


def code(secret_b32: str, t: float | None = None) -> str:
    return _hotp(_cle(secret_b32), int((time.time() if t is None else t) // PAS))


def verifier(secret_b32: str, saisi: str, t: float | None = None, fenetre: int = 1) -> bool:
    saisi = re.sub(r"[\s-]", "", str(saisi))
    if not re.fullmatch(r"\d{%d}" % CHIFFRES, saisi):
        return False
    cle = _cle(secret_b32)
    pas = int((time.time() if t is None else t) // PAS)
    ok = False
    for ecart in range(-fenetre, fenetre + 1):
        ok |= hmac.compare_digest(_hotp(cle, pas + ecart), saisi)
    return ok


def uri(secret_b32: str) -> str:
    return f"otpauth://totp/AME%20Tech:tech.ame.paris?secret={secret_b32}&issuer=AME%20Tech&digits={CHIFFRES}&period={PAS}"


def enrolement(secret_b32: str) -> str:
    return PREFIXE + secret_b32


def lire_enrolement(texte: str) -> str:
    texte = str(texte).strip()
    if texte.startswith(PREFIXE):
        secret = texte[len(PREFIXE):]
    elif texte.startswith("otpauth://totp/"):
        secret = parse_qs(urlsplit(texte).query).get("secret", [""])[0]
    else:
        raise ValueError("Code d'enrôlement inconnu")
    secret = secret.strip().upper()
    if not re.fullmatch(r"[A-Z2-7]{16,64}", secret):
        raise ValueError("Secret invalide")
    _cle(secret)
    return secret
