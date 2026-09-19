"""Manejo de contrasenas del ciudadano (docs/especificacion.md, "Acceso del ciudadano").

Algoritmo: Argon2id, via argon2-cffi.
"""

from __future__ import annotations

from argon2 import PasswordHasher

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)
