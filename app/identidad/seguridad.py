"""Manejo de contrasenas del ciudadano (docs/especificacion.md, "Acceso del ciudadano").

Algoritmo: Argon2id, via argon2-cffi.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

_hasher = PasswordHasher()

# "Parametros y limites": minimo 10 caracteres, con al menos una letra y un digito.
# Unica definicion del proyecto -- CU-01 (registro) y el establecimiento de contrasena
# de primer acceso (CU-16) comparten esta misma regla.
_PATRON_PASSWORD = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{10,}$")


def validar_formato_password(valor: str) -> str:
    if not _PATRON_PASSWORD.match(valor):
        raise ValueError("la contraseña debe tener mínimo 10 caracteres, con al menos una letra y un dígito")
    return valor


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verificar_password(password_hash: str | None, password: str) -> bool:
    # NULL = ciudadano recibido por transferencia (CU-16) que aun no define contrasena
    # local: nunca coincide, en vez de reventar contra el binding de argon2.
    if password_hash is None:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
