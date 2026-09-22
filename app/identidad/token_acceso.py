"""Secretos opacos de alta entropia, buscables por igualdad de su hash.

Dos usos en el proyecto: el token de un solo uso con el que un ciudadano recibido por
transferencia (sin contrasena local, ver `Ciudadano.password_hash`) establece su
contrasena inicial; y la clave de API de una entidad emisora (`EntidadEmisora`,
CU-13) autenticada por el encabezado `X-Api-Key`.

A diferencia del token de sesion (`app/identidad/token.py`, un JWT firmado y
autocontenido), esto es un valor aleatorio opaco: se entrega en claro una sola vez (por
correo, o al darse de alta) y solo su hash se guarda. El hash es determinista (SHA-256,
sin sal) a proposito -- a diferencia de una contrasena, este valor tiene entropia alta
de por si (256 bits generados por `secrets`), asi que no hace falta un hash costoso
como Argon2; lo que si hace falta es poder buscarlo por igualdad en la base
(`WHERE hash = ...`), que un hash con sal aleatoria no permite.
"""

from __future__ import annotations

import hashlib
import secrets


def generar_token() -> tuple[str, str]:
    """Devuelve (token_en_claro, hash). El token en claro se entrega una sola vez (por
    correo) y no se guarda en ningun lado; solo el hash queda persistido."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
