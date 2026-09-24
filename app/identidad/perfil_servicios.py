"""Capa de servicios de perfil y segundo factor: la lógica de negocio de
`app.identidad.perfil` (datos del ciudadano y cuota) y `app.identidad.perfil_totp`
(enrolamiento, confirmación y baja del TOTP), compartida con las pantallas del portal
(`app.portal`). Ver AD-11 (docs/especificacion.md).

Devuelve instancias de `Ciudadano` ya refrescadas, igual que el resto de la capa de
servicios -- el portal lee sus columnas directamente en vez de pasar por los modelos
`Respuesta*` de la API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.seguridad import verificar_password
from app.identidad.totp import generar_secreto, uri_otpauth, verificar_codigo
from app.models import Auditoria, Ciudadano, Documento, EstadoCiudadano, EstadoDocumento, EstadoOutbox, EstadoTotp, Outbox


async def _usado_bytes(session: AsyncSession, ciudadano_id: int) -> int:
    # Mismo calculo que la carga de documentos (app.documentos.servicios): solo cuenta
    # lo temporal y ACTIVO -- los certificados no consumen cuota, y lo reemplazado
    # (CU-10) o eliminado (CU-08) ya no forma parte de la carpeta vigente.
    r = await session.execute(
        select(func.coalesce(func.sum(Documento.tamano_bytes), 0))
        .select_from(Documento)
        .where(
            Documento.ciudadano_id == ciudadano_id,
            Documento.certificado.is_(False),
            Documento.estado == EstadoDocumento.ACTIVO,
        )
    )
    return int(r.scalar_one())


async def obtener_perfil(*, ciudadano_id: int) -> tuple[Ciudadano, int, int]:
    """Devuelve `(ciudadano, cuota_bytes, usado_bytes)`."""
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None
        usado = await _usado_bytes(session, ciudadano.id)
    return ciudadano, cfg.cuota_ciudadano_bytes, usado


async def estado_afiliacion(*, ciudadano_id: int) -> str | None:
    """Para el perfil (portal): en qué va la afiliación del ciudadano ante el MinTIC,
    mientras el registro no la registra en ningún lado visible. El registro deja al
    ciudadano en `PENDIENTE_CENTRALIZADOR` y encola `registerCitizen`; la bandeja de
    salida lo pasa a `ACTIVO` en cuanto el centralizador confirma (CU-01, paso 7).

    Devuelve `"pendiente"` mientras la fila más reciente de `registerCitizen` para
    esta cédula sigue `PENDIENTE`/`EN_PROCESO` (o directamente no existe todavía, el
    instante entre crear al ciudadano y que la bandeja de salida la reclame),
    `"fallido"` si esa fila ya agotó sus reintentos (`FALLIDO`) -- CU-01 dice
    explícitamente que el ciudadano se queda en `PENDIENTE_CENTRALIZADOR` para
    siempre en ese caso, así que sin esto el portal lo dejaría pareciendo "pendiente"
    indefinidamente sin decir qué pasó -- o `None` si el ciudadano ya no está en
    `PENDIENTE_CENTRALIZADOR` (nada que mostrar: ya se resolvió, o todavía ni llega a
    esa etapa -- `PENDIENTE_VERIFICACION`, antes incluso de la Registraduría, no es
    esto)."""
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None
        if ciudadano.estado != EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
            return None

        resultado = await session.execute(
            select(Outbox.estado)
            .where(Outbox.operacion == "registerCitizen", Outbox.payload["cedula"].astext == str(ciudadano_id))
            .order_by(Outbox.creado_en.desc())
            .limit(1)
        )
        estado_outbox = resultado.scalar_one_or_none()
        return "fallido" if estado_outbox == EstadoOutbox.FALLIDO else "pendiente"


async def actualizar_perfil(
    *,
    ciudadano_id: int,
    direccion: str | None,
    telefono: str | None,
    email_personal: str | None,
    correlation_id: str | None,
) -> tuple[Ciudadano, int, int]:
    """CU-03 no incluido: la cédula y `email_carpeta` son permanentes (AD-10) y no
    aceptan cambio por esta vía."""
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

        cambios: dict[str, str] = {}
        if direccion is not None:
            ciudadano.direccion = direccion
            cambios["direccion"] = direccion
        if telefono is not None:
            ciudadano.telefono = telefono
            cambios["telefono"] = telefono
        if email_personal is not None:
            ciudadano.email_personal = email_personal
            cambios["email_personal"] = email_personal

        if cambios:
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="perfil.actualizado",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle=cambios,
                )
            )
            await session.commit()
            await session.refresh(ciudadano)

        usado = await _usado_bytes(session, ciudadano.id)
    return ciudadano, cfg.cuota_ciudadano_bytes, usado


# --- segundo factor (TOTP) ----------------------------------------------------------


def _pendiente_vencido(ciudadano: Ciudadano, cfg) -> bool:
    if ciudadano.totp_secret_actualizado_en is None:
        return True
    limite = ciudadano.totp_secret_actualizado_en + timedelta(minutes=cfg.totp_pendiente_minutos)
    return datetime.now(timezone.utc) >= limite


async def iniciar_enrolamiento_totp(*, ciudadano_id: int, correlation_id: str | None) -> tuple[str, str]:
    """Devuelve `(secreto, uri)`. Una nueva solicitud reemplaza cualquier enrolamiento
    pendiente anterior, e incluso si el segundo factor ya estaba habilitado, lo
    reinicia desde cero."""
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

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
                correlation_id=correlation_id,
                detalle={},
            )
        )
        await session.commit()

        return secreto, uri_otpauth(email_carpeta=ciudadano.email_carpeta, secreto=secreto)


async def estado_totp_pendiente(*, ciudadano_id: int) -> tuple[str, str] | None:
    """Si hay un enrolamiento PENDIENTE vigente (sin vencer), devuelve `(secreto, uri)`
    leídos de la base -- permite recargar la pantalla de confirmación sin perder el
    secreto, que solo se muestra una vez por enrolamiento pero sigue en la base
    mientras esté PENDIENTE."""
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None
        if ciudadano.totp_estado != EstadoTotp.PENDIENTE or _pendiente_vencido(ciudadano, cfg):
            return None
        return ciudadano.totp_secret, uri_otpauth(email_carpeta=ciudadano.email_carpeta, secreto=ciudadano.totp_secret)


async def confirmar_enrolamiento_totp(*, ciudadano_id: int, codigo: str, correlation_id: str | None) -> None:
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

        if ciudadano.totp_estado != EstadoTotp.PENDIENTE or _pendiente_vencido(ciudadano, cfg):
            if ciudadano.totp_estado == EstadoTotp.PENDIENTE:
                ciudadano.totp_secret = None
                ciudadano.totp_estado = None
                ciudadano.totp_secret_actualizado_en = None
                await session.commit()
            raise ErrorDeNegocio("ESTADO_INVALIDO", "No hay una solicitud de enrolamiento vigente.")

        if not verificar_codigo(ciudadano, codigo, cfg):
            session.add(
                Auditoria(
                    actor=str(ciudadano.id),
                    accion="totp.confirmacion_invalida",
                    recurso=str(ciudadano.id),
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle={},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Código del segundo factor inválido o vencido.")

        ciudadano.totp_estado = EstadoTotp.HABILITADO
        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="totp.habilitado",
                recurso=str(ciudadano.id),
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={},
            )
        )
        await session.commit()


async def deshabilitar_totp(*, ciudadano_id: int, password: str, codigo: str, correlation_id: str | None) -> None:
    cfg = get_config()
    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None

        if ciudadano.totp_estado != EstadoTotp.HABILITADO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El segundo factor no está habilitado.")

        if not verificar_password(ciudadano.password_hash, password):
            raise ErrorDeNegocio("CREDENCIALES_INVALIDAS", "Usuario o contraseña incorrectos.")

        if not verificar_codigo(ciudadano, codigo, cfg):
            raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Código del segundo factor inválido o vencido.")

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
                correlation_id=correlation_id,
                detalle={},
            )
        )
        await session.commit()
