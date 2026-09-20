"""Enrolamiento del segundo factor (docs/especificacion.md, "Segundo factor").

`POST /api/v1/perfil/totp` inicia el enrolamiento, `POST .../confirmar` lo activa y
`DELETE /api/v1/perfil/totp` lo deshabilita. Los tres exigen sesion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.dependencias import ciudadano_actual
from app.identidad.seguridad import verificar_password
from app.identidad.totp import generar_secreto, uri_otpauth, verificar_codigo
from app.models import Auditoria, Ciudadano, EstadoTotp

router = APIRouter(prefix="/api/v1/perfil/totp", tags=["segundo factor"])


class RespuestaEnrolamiento(BaseModel):
    secreto: str
    uri: str


class SolicitudConfirmacion(BaseModel):
    codigo: str = Field(min_length=1)


class SolicitudDeshabilitar(BaseModel):
    password: str = Field(min_length=1)
    codigo: str = Field(min_length=1)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _pendiente_vencido(ciudadano: Ciudadano, cfg) -> bool:
    if ciudadano.totp_secret_actualizado_en is None:
        return True
    limite = ciudadano.totp_secret_actualizado_en + timedelta(minutes=cfg.totp_pendiente_minutos)
    return datetime.now(timezone.utc) >= limite


@router.post("", response_model=RespuestaEnrolamiento, status_code=201)
async def iniciar_enrolamiento(request: Request, actual: Ciudadano = Depends(ciudadano_actual)) -> RespuestaEnrolamiento:
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None

        # "Una nueva solicitud de enrolamiento reemplaza cualquier secreto pendiente
        # anterior" -- tambien si ya estaba HABILITADO, se re-enrola desde cero.
        secreto = generar_secreto()
        ciudadano.totp_secret = secreto
        ciudadano.totp_estado = EstadoTotp.PENDIENTE
        ciudadano.totp_secret_actualizado_en = datetime.now(timezone.utc)
        ciudadano.totp_ultimo_paso = None

        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="totp.enrolamiento_iniciado",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=_correlation_id(request),
                detalle={},
            )
        )
        await session.commit()

        return RespuestaEnrolamiento(
            secreto=secreto, uri=uri_otpauth(email_carpeta=ciudadano.email_carpeta, secreto=secreto)
        )


@router.post("/confirmar", status_code=204, response_model=None)
async def confirmar_enrolamiento(
    solicitud: SolicitudConfirmacion, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None

        if ciudadano.totp_estado != EstadoTotp.PENDIENTE or _pendiente_vencido(ciudadano, cfg):
            # El secreto vencido se descarta (docs/especificacion.md, "Enrolamiento").
            if ciudadano.totp_estado == EstadoTotp.PENDIENTE:
                ciudadano.totp_secret = None
                ciudadano.totp_estado = None
                ciudadano.totp_secret_actualizado_en = None
                await session.commit()
            raise ErrorDeNegocio("ESTADO_INVALIDO", "No hay una solicitud de enrolamiento vigente.")

        if not verificar_codigo(ciudadano, solicitud.codigo, cfg):
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="totp.confirmacion_invalida",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=_correlation_id(request),
                    detalle={},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Codigo del segundo factor invalido o vencido.")

        ciudadano.totp_estado = EstadoTotp.HABILITADO
        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="totp.habilitado",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=_correlation_id(request),
                detalle={},
            )
        )
        await session.commit()


@router.delete("", status_code=204, response_model=None)
async def deshabilitar_totp(
    solicitud: SolicitudDeshabilitar, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, actual.id)
        assert ciudadano is not None

        if ciudadano.totp_estado != EstadoTotp.HABILITADO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El segundo factor no esta habilitado.")

        if not verificar_password(ciudadano.password_hash, solicitud.password):
            raise ErrorDeNegocio("CREDENCIALES_INVALIDAS", "Usuario o contrasena incorrectos.")

        if not verificar_codigo(ciudadano, solicitud.codigo, cfg):
            raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Codigo del segundo factor invalido o vencido.")

        ciudadano.totp_secret = None
        ciudadano.totp_estado = None
        ciudadano.totp_secret_actualizado_en = None
        ciudadano.totp_ultimo_paso = None

        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="totp.deshabilitado",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=_correlation_id(request),
                detalle={},
            )
        )
        await session.commit()
