"""Token de sesion (docs/especificacion.md, "Acceso del ciudadano": JWT RS256 con
vencimiento). La llave privada firma, la publica verifica -- ningun otro modulo necesita
la privada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_config
from app.models import Ciudadano

ALGORITMO = "RS256"


class TokenInvalido(Exception):
    """El token esta ausente, mal formado, vencido o con firma invalida."""


@dataclass(frozen=True)
class TokenEmitido:
    access_token: str
    expires_in: int


def emitir_token(ciudadano: Ciudadano) -> TokenEmitido:
    cfg = get_config()
    ahora = datetime.now(timezone.utc)
    vigencia = timedelta(minutes=cfg.jwt_minutos)
    claims = {
        "sub": str(ciudadano.id),
        "email": ciudadano.email_carpeta,
        "iat": ahora,
        "exp": ahora + vigencia,
    }
    token = jwt.encode(claims, cfg.jwt_llave_privada, algorithm=ALGORITMO)
    return TokenEmitido(access_token=token, expires_in=int(vigencia.total_seconds()))


def decodificar_token(token: str) -> int:
    """Devuelve la cedula (`sub`) del token. Lanza TokenInvalido si no es utilizable."""
    cfg = get_config()
    try:
        claims = jwt.decode(token, cfg.jwt_llave_publica, algorithms=[ALGORITMO])
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise TokenInvalido(str(exc)) from exc
