"""Ed25519 en Python pur (bibliothèque standard seulement).

Transcription de l'algorithme de référence de la RFC 8032, section 5.1 et
annexe A (coordonnées étendues, vérification sans multiplication par le
cofacteur). Lent comparé à une implémentation native (quelques dizaines de
millisecondes par opération), largement suffisant pour vérifier un manifeste
de mise à jour.

API :
    cle_publique(graine: bytes) -> bytes          # graine privée de 32 octets
    signer(graine: bytes, message: bytes) -> bytes   # signature de 64 octets
    verifier(publique: bytes, message: bytes, signature: bytes) -> bool

`verifier` ne lève jamais d'exception sur une entrée hostile : toute donnée
mal formée donne False. `signer` ne sert qu'aux outils de publication ; il
n'est pas à temps constant et ne doit pas tourner sur une machine partagée.
"""

from __future__ import annotations

import hashlib

__all__ = ["cle_publique", "signer", "verifier"]

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_RACINE_MOINS_UN = pow(2, (_P - 1) // 4, _P)


def _inverse(x: int) -> int:
    return pow(x, _P - 2, _P)


def _sha512_mod_l(donnees: bytes) -> int:
    return int.from_bytes(hashlib.sha512(donnees).digest(), "little") % _L


def _addition(a: tuple, b: tuple) -> tuple:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    ca = (y1 - x1) * (y2 - x2) % _P
    cb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    cd = 2 * z1 * z2 % _P
    e, f, g, h = cb - ca, cd - cc, cd + cc, cb + ca
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiplication(scalaire: int, point: tuple) -> tuple:
    resultat = (0, 1, 1, 0)
    while scalaire > 0:
        if scalaire & 1:
            resultat = _addition(resultat, point)
        point = _addition(point, point)
        scalaire >>= 1
    return resultat


def _egaux(a: tuple, b: tuple) -> bool:
    if (a[0] * b[2] - b[0] * a[2]) % _P:
        return False
    return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _retrouver_x(y: int, signe: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inverse(_D * y * y + 1) % _P
    if x2 == 0:
        return None if signe else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _RACINE_MOINS_UN % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != signe:
        x = _P - x
    return x


_GY = 4 * _inverse(5) % _P
_GX = _retrouver_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compresser(point: tuple) -> bytes:
    zinv = _inverse(point[2])
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompresser(donnees: bytes) -> tuple | None:
    if len(donnees) != 32:
        return None
    y = int.from_bytes(donnees, "little")
    signe = y >> 255
    y &= (1 << 255) - 1
    x = _retrouver_x(y, signe)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _developper(graine: bytes) -> tuple[int, bytes]:
    if not isinstance(graine, (bytes, bytearray)) or len(graine) != 32:
        raise ValueError("La graine Ed25519 doit faire 32 octets.")
    h = hashlib.sha512(bytes(graine)).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def cle_publique(graine: bytes) -> bytes:
    """Clé publique (32 octets) correspondant à une graine privée de 32 octets."""
    a, _ = _developper(graine)
    return _compresser(_multiplication(a, _G))


def signer(graine: bytes, message: bytes) -> bytes:
    """Signature Ed25519 (64 octets) de `message`."""
    a, prefixe = _developper(graine)
    publique = _compresser(_multiplication(a, _G))
    r = _sha512_mod_l(prefixe + bytes(message))
    r_comp = _compresser(_multiplication(r, _G))
    h = _sha512_mod_l(r_comp + publique + bytes(message))
    s = (r + h * a) % _L
    return r_comp + int.to_bytes(s, 32, "little")


def verifier(publique: bytes, message: bytes, signature: bytes) -> bool:
    """True seulement si `signature` est une signature valide de `message`."""
    try:
        if not isinstance(publique, (bytes, bytearray)) or len(publique) != 32:
            return False
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            return False
        if not isinstance(message, (bytes, bytearray)):
            return False
        publique, signature, message = bytes(publique), bytes(signature), bytes(message)
        point_a = _decompresser(publique)
        if point_a is None:
            return False
        r_comp = signature[:32]
        point_r = _decompresser(r_comp)
        if point_r is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        h = _sha512_mod_l(r_comp + publique + message)
        return _egaux(_multiplication(s, _G), _addition(point_r, _multiplication(h, point_a)))
    except Exception:  # entrée hostile : jamais d'exception vers l'appelant
        return False
