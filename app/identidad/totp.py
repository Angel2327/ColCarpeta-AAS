"""Segundo factor (docs/especificacion.md, "Segundo factor").

TOTP_MODO=simulado: el enrolamiento genera un secreto y una URI reales, igual que en modo
`real`, pero la verificacion solo acepta TOTP_CODIGO_SIMULADO -- no se valida el secreto
ni se rastrea reuso, porque un codigo fijo no tiene un "periodo" que reutilizar.

TOTP_MODO=real: verificacion con pyotp contra el secreto del ciudadano, con la tolerancia
de un periodo que pide el spec, evitando aceptar dos veces el mismo periodo
(`ciudadano.totp_ultimo_paso`).
"""

from __future__ import annotations

import hmac
import time

import pyotp

from app.config import Config
from app.models import Ciudadano


def generar_secreto() -> str:
    return pyotp.random_base32()


def uri_otpauth(*, email_carpeta: str, secreto: str) -> str:
    return f"otpauth://totp/ColCarpeta:{email_carpeta}?secret={secreto}&issuer=ColCarpeta"


def verificar_codigo(ciudadano: Ciudadano, codigo: str, cfg: Config) -> bool:
    """Verifica `codigo` contra el secreto pendiente o habilitado de `ciudadano`.

    En modo real, si el codigo es valido, marca `ciudadano.totp_ultimo_paso` (el llamador
    debe conservar el cambio, p. ej. dejando que la sesion lo confirme al hacer commit).
    """
    if not codigo:
        return False
    if cfg.totp_modo == "simulado":
        return hmac.compare_digest(codigo, cfg.totp_codigo_simulado)

    if not ciudadano.totp_secret:
        return False
    totp = pyotp.TOTP(ciudadano.totp_secret, interval=cfg.totp_periodo_segundos)
    ahora = time.time()
    paso_actual = int(ahora // cfg.totp_periodo_segundos)
    for delta in range(-cfg.totp_tolerancia_periodos, cfg.totp_tolerancia_periodos + 1):
        paso = paso_actual + delta
        if paso == ciudadano.totp_ultimo_paso:
            continue  # ya se uso este periodo, no se acepta de nuevo
        candidato = totp.at(paso * cfg.totp_periodo_segundos)
        if hmac.compare_digest(candidato, codigo):
            ciudadano.totp_ultimo_paso = paso
            return True
    return False
