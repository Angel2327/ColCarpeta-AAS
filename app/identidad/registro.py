"""CU-01: registro del ciudadano.

Ver docs/especificacion.md: "Flujos a implementar" (flujo 1), "Contrato de la API
propia" (POST /api/v1/registro) y "Flujos alternos y de excepcion" (CU-01, A1-A2, E1-E6).

Alcance de esta implementacion: el endpoint, la verificacion de identidad contra la
Registraduria simulada, la llamada sincrona a validateCitizen, la generacion de la
cuenta de correo y la escritura en outbox. El paso 9 (notificar al correo personal) lo
dispara `_activar_ciudadano` en app.interoperabilidad.outbox cuando registerCitizen
confirma el registro, no esta ruta -- el registro puede terminar en
PENDIENTE_CENTRALIZADOR sin que eso llegue a pasar (E5/E6). La carga del documento de
identidad (paso 8) pertenece a Documentos, que sigue "Pendiente".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.correo import generar_email_carpeta
from app.identidad.registraduria_cliente import RegistraduriaNoDisponible, verificar_identidad
from app.identidad.seguridad import hash_password, validar_formato_password
from app.interoperabilidad import CentralizadorNoDisponible, validar_ciudadano
from app.models import Auditoria, Ciudadano, EstadoCiudadano, Outbox

router = APIRouter(prefix="/api/v1", tags=["ciudadano"])

# Intentos totales para las dos llamadas sincronas del flujo (Registraduria y
# validateCitizen). No es la politica de reintentos de la bandeja de salida (esa es para
# llamadas asincronas de varios minutos): aqui el ciudadano esta esperando la respuesta.
REINTENTOS_SINCRONOS = 2
ESPERA_ENTRE_REINTENTOS_SEGUNDOS = 1.0

ESTADOS_YA_REGISTRADO = (
    EstadoCiudadano.ACTIVO,
    EstadoCiudadano.EN_TRANSFERENCIA,
    EstadoCiudadano.TRASLADADO,
)


class SolicitudRegistro(BaseModel):
    cedula: int = Field(gt=0)
    nombre: str = Field(min_length=1, max_length=255)
    direccion: str = Field(min_length=1, max_length=500)
    email_personal: EmailStr
    telefono: str = Field(min_length=1, max_length=30)
    password: str

    @field_validator("password")
    @classmethod
    def _validar_password(cls, valor: str) -> str:
        return validar_formato_password(valor)


class RespuestaRegistro(BaseModel):
    id: int
    nombre: str
    email_carpeta: str
    estado: EstadoCiudadano


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@router.post("/registro", response_model=RespuestaRegistro, status_code=201)
async def registrar(solicitud: SolicitudRegistro, request: Request) -> RespuestaRegistro:
    """Registra un nuevo ciudadano en ColCarpeta.

    Verifica la identidad del ciudadano y genera su dirección de carpeta
    (`email_carpeta`), que es permanente y no puede cambiarse después. El estado
    devuelto puede ser `PENDIENTE_CENTRALIZADOR`: es normal justo después de
    registrarse, mientras se completa la afiliación ante el sistema nacional; la
    carpeta se considera activa cuando el estado pasa a `ACTIVO`.

    Devuelve 409 si la cédula ya está registrada en ColCarpeta o ya está afiliada a
    otro operador, 409 si no fue posible confirmar la identidad del ciudadano, o 503
    si el servicio de verificación de identidad o el sistema nacional no están
    disponibles en este momento.
    """
    cfg = get_config()
    correlation_id = _correlation_id(request)

    async with SessionLocal() as session:
        existente = await session.get(Ciudadano, solicitud.cedula)

        if existente is not None and existente.estado in ESTADOS_YA_REGISTRADO:
            raise ErrorDeNegocio("CIUDADANO_YA_REGISTRADO", "La cedula ya esta registrada en ColCarpeta.")

        if existente is not None and existente.estado == EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
            # A1: se reanuda sin duplicar el registro ni la entrada de bandeja de salida.
            return RespuestaRegistro(
                id=existente.id,
                nombre=existente.nombre,
                email_carpeta=existente.email_carpeta,
                estado=existente.estado,
            )

        # --- E2/E3: verificacion de identidad contra la Registraduria -----------------
        resultado_identidad = None
        error_registraduria: RegistraduriaNoDisponible | None = None
        for intento in range(REINTENTOS_SINCRONOS):
            try:
                resultado_identidad = await verificar_identidad(
                    request.app, cedula=solicitud.cedula, nombre=solicitud.nombre
                )
                error_registraduria = None
                break
            except RegistraduriaNoDisponible as exc:
                error_registraduria = exc
                if intento < REINTENTOS_SINCRONOS - 1:
                    await asyncio.sleep(ESPERA_ENTRE_REINTENTOS_SEGUNDOS)

        if error_registraduria is not None:
            # E3: la Registraduria no respondio. Se reserva la cedula en
            # PENDIENTE_VERIFICACION para que un nuevo intento de registro reanude la
            # verificacion, y se informa al ciudadano.
            if existente is None:
                session.add(
                    Ciudadano(
                        id=solicitud.cedula,
                        nombre=solicitud.nombre,
                        direccion=solicitud.direccion,
                        email_personal=str(solicitud.email_personal),
                        telefono=solicitud.telefono,
                        password_hash=hash_password(solicitud.password),
                        estado=EstadoCiudadano.PENDIENTE_VERIFICACION,
                        identidad_verificada=False,
                    )
                )
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="registro.registraduria_no_disponible",
                    recurso=str(solicitud.cedula),
                    ciudadano_id=solicitud.cedula,
                    correlation_id=correlation_id,
                    detalle={"error": str(error_registraduria)},
                )
            )
            await session.commit()
            raise ErrorDeNegocio(
                "REGISTRADURIA_NO_DISPONIBLE",
                "No fue posible verificar tu identidad en este momento. Intenta de nuevo en unos minutos.",
            )

        assert resultado_identidad is not None
        if not resultado_identidad.verificado:
            # E2: identidad no confirmada. No se persiste el ciudadano; se deja
            # constancia en auditoria del resultado de la consulta.
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="registro.identidad_no_verificada",
                    recurso=str(solicitud.cedula),
                    correlation_id=correlation_id,
                    detalle={"motivo": resultado_identidad.motivo},
                )
            )
            await session.commit()
            raise ErrorDeNegocio("IDENTIDAD_NO_VERIFICADA", "La Registraduria no pudo confirmar tu identidad.")

        # --- E1/E4: validateCitizen, unica llamada sincrona al centralizador ----------
        resultado_validacion = None
        error_centralizador: CentralizadorNoDisponible | None = None
        for intento in range(REINTENTOS_SINCRONOS):
            try:
                resultado_validacion = await validar_ciudadano(solicitud.cedula)
                error_centralizador = None
                break
            except CentralizadorNoDisponible as exc:
                error_centralizador = exc
                if intento < REINTENTOS_SINCRONOS - 1:
                    await asyncio.sleep(ESPERA_ENTRE_REINTENTOS_SEGUNDOS)

        if error_centralizador is not None:
            # E4: agotados los reintentos sincronos, se informa indisponibilidad
            # temporal y no se persiste nada.
            raise ErrorDeNegocio(
                "CENTRALIZADOR_NO_DISPONIBLE",
                "El centralizador del MinTIC no esta disponible en este momento. Intenta de nuevo en unos minutos.",
            )

        assert resultado_validacion is not None
        if not resultado_validacion.disponible:
            # E1: ya afiliado a otro operador. No se persiste nada; se informa el
            # operador actual y se deja constancia en auditoria.
            session.add(
                Auditoria(
                    actor="sistema",
                    accion="registro.ya_afiliado",
                    recurso=str(solicitud.cedula),
                    correlation_id=correlation_id,
                    detalle={"operador_actual": resultado_validacion.mensaje},
                )
            )
            await session.commit()
            raise ErrorDeNegocio(
                "CIUDADANO_YA_AFILIADO",
                "El ciudadano ya esta afiliado a otro operador.",
                detalle={"operador_actual": resultado_validacion.mensaje},
            )

        # --- Camino basico (y reanudacion de PENDIENTE_VERIFICACION): crear al
        # ciudadano, generar su correo y encolar registerCitizen. --------------------
        anio = datetime.now(timezone.utc).year
        email_carpeta = await generar_email_carpeta(
            session, nombre_completo=solicitud.nombre, anio=anio, dominio=cfg.dominio_carpeta
        )

        if existente is not None:
            ciudadano = existente
            ciudadano.nombre = solicitud.nombre
            ciudadano.direccion = solicitud.direccion
            ciudadano.email_personal = str(solicitud.email_personal)
            ciudadano.telefono = solicitud.telefono
            ciudadano.password_hash = hash_password(solicitud.password)
        else:
            ciudadano = Ciudadano(
                id=solicitud.cedula,
                nombre=solicitud.nombre,
                direccion=solicitud.direccion,
                email_personal=str(solicitud.email_personal),
                telefono=solicitud.telefono,
                password_hash=hash_password(solicitud.password),
            )
            session.add(ciudadano)

        ciudadano.email_carpeta = email_carpeta
        ciudadano.estado = EstadoCiudadano.PENDIENTE_CENTRALIZADOR
        ciudadano.identidad_verificada = True

        session.add(
            Outbox(
                operacion="registerCitizen",
                payload={
                    "cedula": solicitud.cedula,
                    "nombre": solicitud.nombre,
                    "direccion": solicitud.direccion,
                    "email": email_carpeta,
                    "correlation_id": correlation_id,
                    # Paso 9 de CU-01 ("el sistema notifica al correo personal"): solo el
                    # registro real lo pide. _activar_ciudadano (outbox) reencola esta
                    # misma operacion para recuperar a un ciudadano tras un envio o una
                    # recepcion fallidos (CU-03/CU-16), y ahi no aplica.
                    "notificar_registro": True,
                },
            )
        )
        session.add(
            Auditoria(
                actor=str(solicitud.cedula),
                accion="registro.creado",
                recurso=str(solicitud.cedula),
                ciudadano_id=solicitud.cedula,
                correlation_id=correlation_id,
                detalle={"email_carpeta": email_carpeta},
            )
        )

        await session.commit()

        return RespuestaRegistro(
            id=ciudadano.id,
            nombre=ciudadano.nombre,
            email_carpeta=ciudadano.email_carpeta,
            estado=ciudadano.estado,
        )
