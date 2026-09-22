"""Dependencia de autenticacion para las rutas que exigen sesion.

`NO_AUTENTICADO` no esta en la tabla de "Formato de errores" del spec: se agrego para
distinguir "no hay token / token invalido o vencido" de `CREDENCIALES_INVALIDAS`, que la
especificacion reserva para usuario o contrasena incorrectos en el login.
"""

from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.token import TokenInvalido, decodificar_token
from app.models import Ciudadano

_bearer = HTTPBearer(auto_error=False)


async def resolver_usuario(session: AsyncSession, usuario: str) -> Ciudadano | None:
    """Busca un ciudadano por cedula o por email_carpeta -- el mismo criterio que acepta
    el campo `usuario` del inicio de sesion. Compartido entre el login y el reenvio del
    token de primer acceso."""
    usuario = usuario.strip()
    if usuario.isdigit():
        return await session.get(Ciudadano, int(usuario))
    resultado = await session.execute(select(Ciudadano).where(Ciudadano.email_carpeta == usuario.lower()))
    return resultado.scalar_one_or_none()


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
