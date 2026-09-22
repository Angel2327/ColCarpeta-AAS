"""CU-02: inicio y cierre de sesion.

Ver docs/especificacion.md: "Flujos a implementar" (flujo 2), "Contrato de la API
propia" (POST/DELETE /api/v1/sesion), "Flujos alternos y de excepcion" (CU-02, A1-A2,
E1-E4) y "Parametros y limites" (bloqueo por intentos fallidos).

El bloqueo (E3) y el conteo de intentos fallidos se derivan de `auditoria`: no existe una
tabla propia para eso en el modelo de datos, y auditoria ya registra cada intento con su
origen, asi que sirve como fuente de verdad para la ventana deslizante de 15 minutos.

`DELETE /api/v1/sesion` no invalida el JWT del lado del servidor -- el modelo de datos no
define una tabla de sesiones/tokens revocados -- se limita a exigir un token valido y
dejar constancia del cierre en auditoria; el token sigue siendo tecnicamente valido hasta
que expira por su cuenta.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import ciudadano_actual
from app.identidad.seguridad import verificar_password
from app.identidad.token import emitir_token
from app.identidad.totp import verificar_codigo
from app.models import Auditoria, Ciudadano, EstadoCiudadano, EstadoTotp

router = APIRouter(prefix="/api/v1", tags=["sesion"])

ACCIONES_FALLO_LOGIN = ("sesion.credenciales_invalidas", "sesion.totp_invalido")
ESTADOS_SIN_ACCESO = (EstadoCiudadano.EN_TRANSFERENCIA, EstadoCiudadano.TRASLADADO)


class SolicitudSesion(BaseModel):
    usuario: str = Field(min_length=1)
    password: str = Field(min_length=1)
    codigo_totp: str | None = None


class RespuestaSesion(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    # Extensiones aditivas (no rompen al portal actual) para A1/A2: el contrato
    # documentado solo define los tres campos de arriba.
    segundo_factor_habilitado: bool
    primer_inicio_sesion: bool


def _origen(request: Request) -> str:
    return request.client.host if request.client else "desconocido"


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


async def _resolver_usuario(session: AsyncSession, usuario: str) -> Ciudadano | None:
    usuario = usuario.strip()
    if usuario.isdigit():
        return await session.get(Ciudadano, int(usuario))
    resultado = await session.execute(select(Ciudadano).where(Ciudadano.email_carpeta == usuario.lower()))
    return resultado.scalar_one_or_none()


async def _intentos_fallidos(
    session: AsyncSession, *, ciudadano_id: int | None, origen: str, desde: datetime
) -> tuple[int, int]:
    """(intentos por cedula, intentos por origen) en la ventana [desde, ahora]."""
    por_cedula = 0
    if ciudadano_id is not None:
        r = await session.execute(
            select(func.count()).where(
                Auditoria.accion.in_(ACCIONES_FALLO_LOGIN),
                Auditoria.momento >= desde,
                Auditoria.ciudadano_id == ciudadano_id,
            )
        )
        por_cedula = r.scalar_one()

    r2 = await session.execute(
        select(func.count()).where(
            Auditoria.accion.in_(ACCIONES_FALLO_LOGIN),
            Auditoria.momento >= desde,
            Auditoria.detalle["origen"].astext == origen,
        )
    )
    por_origen = r2.scalar_one()
    return por_cedula, por_origen


def _registrar(
    session: AsyncSession, *, accion: str, ciudadano_id: int | None, correlation_id: str | None, detalle: dict
) -> None:
    session.add(
        Auditoria(
            actor=str(ciudadano_id) if ciudadano_id is not None else "desconocido",
            accion=accion,
            recurso=str(ciudadano_id) if ciudadano_id is not None else None,
            ciudadano_id=ciudadano_id,
            correlation_id=correlation_id,
            detalle=detalle,
        )
    )


@router.post("/sesion", response_model=RespuestaSesion)
async def iniciar_sesion(solicitud: SolicitudSesion, request: Request) -> RespuestaSesion:
    """Inicia sesión y devuelve un token de acceso.

    `usuario` acepta la cédula o la dirección de carpeta (`email_carpeta`). Si el
    ciudadano tiene el segundo factor habilitado, la primera petición sin
    `codigo_totp` responde 428 (la contraseña ya se validó, pero no se emite token);
    se repite la petición completa incluyendo el código para obtenerlo.

    Devuelve 423 si se superó el número de intentos fallidos permitidos (por cédula o
    por origen), 401 si el usuario o la contraseña son incorrectos, 409 si la carpeta
    está en proceso de traslado o ya fue trasladada a otro operador, o 401 si el
    código del segundo factor es incorrecto o venció.
    """
    cfg = get_config()
    origen = _origen(request)
    correlation_id = _correlation_id(request)

    async with SessionLocal() as session:
        ciudadano = await _resolver_usuario(session, solicitud.usuario)
        cedula = ciudadano.id if ciudadano is not None else None

        # --- E3: bloqueo por intentos fallidos, por cedula y por origen -------------
        desde = datetime.now(timezone.utc) - timedelta(minutes=cfg.intentos_login_ventana_minutos)
        por_cedula, por_origen = await _intentos_fallidos(session, ciudadano_id=cedula, origen=origen, desde=desde)
        if por_cedula >= cfg.intentos_login_maximos or por_origen >= cfg.intentos_login_maximos:
            _registrar(
                session,
                accion="sesion.bloqueada",
                ciudadano_id=cedula,
                correlation_id=correlation_id,
                detalle={"origen": origen, "intentos_cedula": por_cedula, "intentos_origen": por_origen},
            )
            await session.commit()
            raise ErrorDeNegocio(
                "CUENTA_BLOQUEADA",
                f"Demasiados intentos fallidos. Intenta de nuevo en {cfg.bloqueo_login_minutos} minutos.",
            )

        # --- E1: credenciales invalidas (mensaje generico, no revela si existe) ----
        credenciales_validas = ciudadano is not None and verificar_password(ciudadano.password_hash, solicitud.password)
        if not credenciales_validas:
            _registrar(
                session,
                accion="sesion.credenciales_invalidas",
                ciudadano_id=cedula,
                correlation_id=correlation_id,
                detalle={"origen": origen},
            )
            await session.commit()
            raise ErrorDeNegocio("CREDENCIALES_INVALIDAS", "Usuario o contrasena incorrectos.")

        assert ciudadano is not None

        # --- E4: estado que impide el acceso ----------------------------------------
        if ciudadano.estado in ESTADOS_SIN_ACCESO:
            _registrar(
                session,
                accion="sesion.estado_invalido",
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={"origen": origen, "estado": ciudadano.estado.value},
            )
            await session.commit()
            mensaje = (
                "Tu carpeta esta en proceso de traslado a otro operador."
                if ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA
                else "Tu carpeta ya fue trasladada a otro operador."
            )
            raise ErrorDeNegocio("ESTADO_INVALIDO", mensaje, detalle={"estado": ciudadano.estado.value})

        # --- A1/segundo factor: exigirlo solo si esta HABILITADO --------------------
        segundo_factor_habilitado = ciudadano.totp_estado == EstadoTotp.HABILITADO
        if segundo_factor_habilitado:
            if not solicitud.codigo_totp:
                # No es E1 ni E2: la contrasena ya se verifico, solo falta el codigo.
                raise ErrorDeNegocio("SEGUNDO_FACTOR_REQUERIDO", "Se requiere el codigo del segundo factor.")

            # --- E2: codigo TOTP invalido o vencido ---------------------------------
            if not verificar_codigo(ciudadano, solicitud.codigo_totp, cfg):
                _registrar(
                    session,
                    accion="sesion.totp_invalido",
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle={"origen": origen},
                )
                await session.commit()
                raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Codigo del segundo factor invalido o vencido.")

        # --- A2: primer inicio de sesion exitoso ------------------------------------
        r = await session.execute(
            select(func.count()).where(Auditoria.accion == "sesion.exitosa", Auditoria.ciudadano_id == ciudadano.id)
        )
        primer_inicio_sesion = r.scalar_one() == 0

        emitido = emitir_token(ciudadano)
        _registrar(
            session,
            accion="sesion.exitosa",
            ciudadano_id=ciudadano.id,
            correlation_id=correlation_id,
            detalle={"origen": origen},
        )
        await session.commit()

        return RespuestaSesion(
            access_token=emitido.access_token,
            expires_in=emitido.expires_in,
            segundo_factor_habilitado=segundo_factor_habilitado,
            primer_inicio_sesion=primer_inicio_sesion,
        )


@router.delete("/sesion", status_code=204, response_model=None)
async def cerrar_sesion(request: Request, ciudadano: Ciudadano = Depends(ciudadano_actual)) -> None:
    """Cierra la sesión actual.

    El token de acceso usado en esta petición no se invalida: sigue siendo válido
    hasta que expira por su cuenta. El cliente debe descartarlo por su lado.
    """
    async with SessionLocal() as session:
        _registrar(
            session,
            accion="sesion.cierre",
            ciudadano_id=ciudadano.id,
            correlation_id=_correlation_id(request),
            detalle={"origen": _origen(request)},
        )
        await session.commit()
