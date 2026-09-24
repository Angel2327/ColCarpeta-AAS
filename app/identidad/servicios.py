"""Capa de servicios de Identidad y Ciudadano: la lógica de negocio de CU-01 (registro)
y CU-02 (inicio de sesión), compartida entre las rutas JSON de la API
(`app.identidad.registro`, `app.identidad.sesion`) y las pantallas HTML del portal
(`app.portal`). Ninguna de las dos capas de transporte duplica esta lógica -- ambas la
piden aquí. Ver AD-11 (docs/especificacion.md) para por qué el portal existe como
plantillas del propio proceso y no como un cliente HTTP de esta misma API.

Los tipos de entrada/salida (`SolicitudRegistro`, `RespuestaRegistro`, `SolicitudSesion`,
`RespuestaSesion`) también viven aquí: son el contrato compartido, no algo propio de la
ruta JSON. Las rutas JSON los usan tal cual como cuerpo/`response_model`; el portal
construye el mismo `Solicitud*` a partir del formulario y renderiza `Respuesta*` como
HTML en vez de JSON.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.errors import ErrorDeNegocio
from app.identidad.correo import generar_email_carpeta
from app.identidad.dependencias import resolver_usuario
from app.identidad.registraduria_cliente import RegistraduriaNoDisponible, verificar_identidad
from app.identidad.seguridad import hash_password, validar_formato_password, verificar_password
from app.identidad.token import emitir_token
from app.identidad.totp import verificar_codigo
from app.interoperabilidad import CentralizadorNoDisponible, validar_ciudadano
from app.models import Auditoria, Ciudadano, EstadoCiudadano, EstadoTotp, OrigenCiudadano, Outbox

REINTENTOS_SINCRONOS = 2
ESPERA_ENTRE_REINTENTOS_SEGUNDOS = 1.0

ESTADOS_YA_REGISTRADO = (
    EstadoCiudadano.ACTIVO,
    EstadoCiudadano.EN_TRANSFERENCIA,
    EstadoCiudadano.TRASLADADO,
)

ACCIONES_FALLO_LOGIN = ("sesion.credenciales_invalidas", "sesion.totp_invalido")
ESTADOS_SIN_ACCESO = (EstadoCiudadano.EN_TRANSFERENCIA, EstadoCiudadano.TRASLADADO)


# --- CU-01: registro --------------------------------------------------------------


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


async def registrar_ciudadano(
    solicitud: SolicitudRegistro, app: FastAPI, *, correlation_id: str | None
) -> RespuestaRegistro:
    """CU-01: verifica identidad, valida ante el centralizador y crea al ciudadano.

    Ver docs/especificacion.md, "Flujos alternos y de excepción" (CU-01, A1-A2, E1-E6)
    para el detalle de cada rama; este es el cuerpo original de
    `POST /api/v1/registro`, movido aquí para que el portal lo comparta.
    """
    cfg = get_config()

    async with SessionLocal() as session:
        existente = await session.get(Ciudadano, solicitud.cedula)

        if existente is not None and existente.estado in ESTADOS_YA_REGISTRADO:
            raise ErrorDeNegocio("CIUDADANO_YA_REGISTRADO", "La cédula ya está registrada en ColCarpeta.")

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
                resultado_identidad = await verificar_identidad(app, cedula=solicitud.cedula, nombre=solicitud.nombre)
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
                        origen=OrigenCiudadano.REGISTRO_DIRECTO,
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
            raise ErrorDeNegocio("IDENTIDAD_NO_VERIFICADA", "La Registraduría no pudo confirmar tu identidad.")

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
                "El centralizador del MinTIC no está disponible en este momento. Intenta de nuevo en unos minutos.",
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
                "El ciudadano ya está afiliado a otro operador.",
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
                origen=OrigenCiudadano.REGISTRO_DIRECTO,
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


# --- CU-02: inicio de sesión -------------------------------------------------------


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


def _origen_peticion(cliente_host: str | None) -> str:
    return cliente_host or "desconocido"


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


def _registrar_auditoria_sesion(
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


async def iniciar_sesion(
    solicitud: SolicitudSesion, *, origen: str | None, correlation_id: str | None
) -> RespuestaSesion:
    """CU-02: valida credenciales, segundo factor y estado, y emite el JWT.

    Ver docs/especificacion.md, "Flujos alternos y de excepción" (CU-02, A1-A2, E1-E4).
    Cuerpo original de `POST /api/v1/sesion`, movido aquí para que el portal lo
    comparta -- incluida la cookie de sesión, que solo la envuelve.
    """
    cfg = get_config()
    origen = _origen_peticion(origen)

    async with SessionLocal() as session:
        ciudadano = await resolver_usuario(session, solicitud.usuario)
        cedula = ciudadano.id if ciudadano is not None else None

        # --- E3: bloqueo por intentos fallidos, por cedula y por origen -------------
        desde = datetime.now(timezone.utc) - timedelta(minutes=cfg.intentos_login_ventana_minutos)
        por_cedula, por_origen = await _intentos_fallidos(session, ciudadano_id=cedula, origen=origen, desde=desde)
        if por_cedula >= cfg.intentos_login_maximos or por_origen >= cfg.intentos_login_maximos:
            _registrar_auditoria_sesion(
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
            _registrar_auditoria_sesion(
                session,
                accion="sesion.credenciales_invalidas",
                ciudadano_id=cedula,
                correlation_id=correlation_id,
                detalle={"origen": origen},
            )
            await session.commit()
            raise ErrorDeNegocio("CREDENCIALES_INVALIDAS", "Usuario o contraseña incorrectos.")

        assert ciudadano is not None

        # --- E4: estado que impide el acceso ----------------------------------------
        if ciudadano.estado in ESTADOS_SIN_ACCESO:
            _registrar_auditoria_sesion(
                session,
                accion="sesion.estado_invalido",
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={"origen": origen, "estado": ciudadano.estado.value},
            )
            await session.commit()
            mensaje = (
                "Tu carpeta está en proceso de traslado a otro operador."
                if ciudadano.estado == EstadoCiudadano.EN_TRANSFERENCIA
                else "Tu carpeta ya fue trasladada a otro operador."
            )
            raise ErrorDeNegocio("ESTADO_INVALIDO", mensaje, detalle={"estado": ciudadano.estado.value})

        # --- A1/segundo factor: exigirlo solo si esta HABILITADO --------------------
        segundo_factor_habilitado = ciudadano.totp_estado == EstadoTotp.HABILITADO
        if segundo_factor_habilitado:
            if not solicitud.codigo_totp:
                # No es E1 ni E2: la contrasena ya se verifico, solo falta el codigo.
                raise ErrorDeNegocio("SEGUNDO_FACTOR_REQUERIDO", "Se requiere el código del segundo factor.")

            # --- E2: codigo TOTP invalido o vencido ---------------------------------
            if not verificar_codigo(ciudadano, solicitud.codigo_totp, cfg):
                _registrar_auditoria_sesion(
                    session,
                    accion="sesion.totp_invalido",
                    ciudadano_id=ciudadano.id,
                    correlation_id=correlation_id,
                    detalle={"origen": origen},
                )
                await session.commit()
                raise ErrorDeNegocio("SEGUNDO_FACTOR_INVALIDO", "Código del segundo factor inválido o vencido.")

        # --- A2: primer inicio de sesion exitoso ------------------------------------
        r = await session.execute(
            select(func.count()).where(Auditoria.accion == "sesion.exitosa", Auditoria.ciudadano_id == ciudadano.id)
        )
        primer_inicio_sesion = r.scalar_one() == 0

        emitido = emitir_token(ciudadano)
        _registrar_auditoria_sesion(
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


async def cerrar_sesion(*, ciudadano_id: int, origen: str | None, correlation_id: str | None) -> None:
    """Deja constancia del cierre de sesión. El JWT no se invalida del lado del
    servidor (no hay tabla de tokens revocados); sigue siendo técnicamente válido
    hasta que expira por su cuenta -- el portal simplemente descarta la cookie."""
    async with SessionLocal() as session:
        _registrar_auditoria_sesion(
            session,
            accion="sesion.cierre",
            ciudadano_id=ciudadano_id,
            correlation_id=correlation_id,
            detalle={"origen": _origen_peticion(origen)},
        )
        await session.commit()
