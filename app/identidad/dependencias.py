"""Dependencia de autenticacion para las rutas que exigen sesion.

`NO_AUTENTICADO` no esta en la tabla de "Formato de errores" del spec: se agrego para
distinguir "no hay token / token invalido o vencido" de `CREDENCIALES_INVALIDAS`, que la
especificacion reserva para usuario o contrasena incorrectos en el login.
"""

from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.token import TokenInvalido, decodificar_token
from app.models import Ciudadano

_bearer = HTTPBearer(auto_error=False)


async def ciudadano_actual(
    credenciales: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Ciudadano:
    if credenciales is None:
        raise ErrorDeNegocio("NO_AUTENTICADO", "Se requiere un token de sesion valido.")
    try:
        cedula = decodificar_token(credenciales.credentials)
    except TokenInvalido as exc:
        raise ErrorDeNegocio("NO_AUTENTICADO", "El token de sesion es invalido o vencio.") from exc

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)

    if ciudadano is None:
        raise ErrorDeNegocio("NO_AUTENTICADO", "El token de sesion es invalido o vencio.")

    return ciudadano
