"""Sesion del portal: viaja en una cookie HttpOnly + Secure + SameSite, nunca en un
token que JavaScript pueda leer -- protege los formularios del portal contra
falsificacion de peticiones entre sitios (AD-11, docs/especificacion.md). El JWT que
lleva la cookie es exactamente el mismo que `app.identidad.token.emitir_token` emite
para la API: el portal no tiene un mecanismo de sesion propio, solo lo transporta
distinto.
"""

from __future__ import annotations

from fastapi import Request
from starlette.responses import Response

from app.config import get_config
from app.db import SessionLocal
from app.identidad.token import TokenInvalido, decodificar_token
from app.models import Ciudadano

NOMBRE_COOKIE = "colcarpeta_sesion"


def fijar_cookie_sesion(response: Response, *, access_token: str, expires_in: int) -> None:
    cfg = get_config()
    response.set_cookie(
        NOMBRE_COOKIE,
        access_token,
        max_age=expires_in,
        httponly=True,
        secure=cfg.portal_cookie_secure,
        samesite="strict",
        path="/",
    )


def borrar_cookie_sesion(response: Response) -> None:
    response.delete_cookie(NOMBRE_COOKIE, path="/")


async def ciudadano_actual_portal(request: Request) -> Ciudadano | None:
    """Como `app.identidad.dependencias.ciudadano_actual`, pero lee el JWT de la cookie
    del portal (no del encabezado Authorization de la API) y devuelve None en vez de
    levantar un error JSON: las pantallas del portal redirigen a iniciar sesion por su
    cuenta en vez de mostrar un 401."""
    token = request.cookies.get(NOMBRE_COOKIE)
    if not token:
        return None
    try:
        cedula = decodificar_token(token)
    except TokenInvalido:
        return None
    async with SessionLocal() as session:
        return await session.get(Ciudadano, cedula)
