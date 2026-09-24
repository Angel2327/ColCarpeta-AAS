"""Sesion de la consola de administracion (RF32-RF37, CU-22): cookie propia,
separada de la del ciudadano (`app.portal.auth`), con las mismas protecciones
(HttpOnly + Secure + SameSite=strict). No hay un modelo de administradores: una sola
credencial en `ADMIN_PASSWORD_HASH` (ver `app.config`), verificada con la misma funcion
que ya usa el ciudadano (`app.identidad.seguridad.verificar_password`) -- nunca se
compara la clave en texto plano. Ver la AD nueva en docs/especificacion.md.

El JWT que lleva esta cookie es independiente del que emite `app.identidad.token`: ese
modulo esta atado a un `Ciudadano` concreto (claims con su cedula y su correo), y aqui
no hay ningun ciudadano detras, solo una credencial global. Se reutilizan las mismas
llaves RS256 de la configuracion (nunca un secreto nuevo que gestionar aparte), con un
claim `sub` fijo ("admin") en vez de una cedula.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request
from starlette.responses import Response

from app.config import get_config

ALGORITMO = "RS256"
SUJETO = "admin"
NOMBRE_COOKIE = "colcarpeta_admin_sesion"


def emitir_token_admin() -> tuple[str, int]:
    cfg = get_config()
    ahora = datetime.now(timezone.utc)
    vigencia = timedelta(minutes=cfg.jwt_minutos)
    claims = {"sub": SUJETO, "iat": ahora, "exp": ahora + vigencia}
    token = jwt.encode(claims, cfg.jwt_llave_privada, algorithm=ALGORITMO)
    return token, int(vigencia.total_seconds())


def _token_valido(token: str) -> bool:
    cfg = get_config()
    try:
        claims = jwt.decode(token, cfg.jwt_llave_publica, algorithms=[ALGORITMO])
    except jwt.PyJWTError:
        return False
    return claims.get("sub") == SUJETO


def fijar_cookie_admin(response: Response, *, access_token: str, expires_in: int) -> None:
    cfg = get_config()
    # path="/admin": a diferencia de la cookie del ciudadano (path="/"), esta ni
    # siquiera se envia hacia el resto del sitio -- una separacion mas, sin costo.
    response.set_cookie(
        NOMBRE_COOKIE,
        access_token,
        max_age=expires_in,
        httponly=True,
        secure=cfg.portal_cookie_secure,
        samesite="strict",
        path="/admin",
    )


def borrar_cookie_admin(response: Response) -> None:
    response.delete_cookie(NOMBRE_COOKIE, path="/admin")


def admin_autenticado(request: Request) -> bool:
    """Como `app.portal.auth.ciudadano_actual_portal`, pero sin ningun ciudadano que
    devolver: la consola no tiene identidad propia por administrador, solo dice si la
    cookie es valida."""
    token = request.cookies.get(NOMBRE_COOKIE)
    if not token:
        return False
    return _token_valido(token)
