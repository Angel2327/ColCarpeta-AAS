"""Capa de servicios de la consola de administracion (RF32-RF37, CU-22): consultas de
solo lectura sobre las tablas que ya existen. Ninguna funcion de este modulo escribe,
borra ni dispara una operacion de negocio (ver la AD nueva en docs/especificacion.md) --
las unicas escrituras son las propias de auditoria (quien inicio sesion, quien vio que
pantalla y cuando).

Nunca se expone el contenido de un documento, solo sus metadatos -- ni un enlace de
descarga ni una URL firmada salen de aqui. Tampoco se expone ningun secreto (hash de
contrasena, secreto TOTP, token de primer acceso, clave de una entidad emisora): los
dataclass de este modulo declaran explicitamente los campos que sí se muestran, nunca
pasan un modelo de SQLAlchemy completo a una plantilla.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.filtros import ESCAPE, columna_sin_acento_contiene, normalizar_filtro_texto, patron_contiene
from app.identidad.seguridad import verificar_password
from app.interoperabilidad.transferencias import resolver_operador_por_host
from app.models import (
    Auditoria,
    Ciudadano,
    Documento,
    EstadoDocumento,
    EstadoOutbox,
    EstadoTransferencia,
    OperadorCache,
    OrigenCiudadano,
    Outbox,
    Transferencia,
)

TAMANO_PAGINA = 20

# Unica accion que cuenta para el bloqueo de la consola: a diferencia del login del
# ciudadano (app.identidad.servicios.ACCIONES_FALLO_LOGIN), aqui no hay un segundo
# factor que pueda fallar aparte -- una sola credencial global.
ACCION_FALLO_ADMIN = "admin.credenciales_invalidas"


# --- acceso: login con el mismo patron de bloqueo por origen que el ciudadano -------


async def _intentos_fallidos_por_origen(session: AsyncSession, *, origen: str, desde: datetime) -> int:
    r = await session.execute(
        select(func.count()).where(
            Auditoria.accion == ACCION_FALLO_ADMIN,
            Auditoria.momento >= desde,
            Auditoria.detalle["origen"].astext == origen,
        )
    )
    return r.scalar_one()


async def registrar_acceso(
    *, accion: str, origen: str, correlation_id: str | None, detalle: dict | None = None
) -> None:
    """Deja constancia en `auditoria` de un evento de la consola -- login, bloqueo, o
    una pantalla vista. "El que vigila tambien se registra": no hay pantalla ni intento
    de acceso que no quede aqui."""
    async with SessionLocal() as session:
        session.add(
            Auditoria(
                actor="admin",
                accion=accion,
                correlation_id=correlation_id,
                detalle={"origen": origen, **(detalle or {})},
            )
        )
        await session.commit()


async def verificar_credenciales_admin(*, password: str, origen: str, correlation_id: str | None) -> str:
    """Valida la contraseña unica de la consola contra `ADMIN_PASSWORD_HASH`, con el
    mismo bloqueo por intentos fallidos que ya usa el inicio de sesion del ciudadano
    (`app.identidad.servicios._intentos_fallidos`), aqui solo por origen -- no hay una
    cedula que acompañe una credencial global. Sin este limite, la consola seria un
    enumerador de ciudadanos para quien adivine la clave a fuerza bruta.

    Devuelve "ok", "bloqueado" o "invalido".
    """
    cfg = get_config()
    async with SessionLocal() as session:
        desde = datetime.now(timezone.utc) - timedelta(minutes=cfg.intentos_login_ventana_minutos)
        fallos = await _intentos_fallidos_por_origen(session, origen=origen, desde=desde)

    if fallos >= cfg.intentos_login_maximos:
        await registrar_acceso(accion="admin.bloqueado", origen=origen, correlation_id=correlation_id)
        return "bloqueado"

    # ADMIN_PASSWORD_HASH vacio por defecto: `or None` hace que verificar_password lo
    # trate igual que un ciudadano sin contrasena (CU-16) -- nunca coincide, la consola
    # queda inutilizable hasta que se configure la variable de verdad.
    if not verificar_password(cfg.admin_password_hash or None, password):
        await registrar_acceso(accion=ACCION_FALLO_ADMIN, origen=origen, correlation_id=correlation_id)
        return "invalido"

    await registrar_acceso(accion="admin.sesion_exitosa", origen=origen, correlation_id=correlation_id)
    return "ok"


# --- 1. resumen ----------------------------------------------------------------------


@dataclass(frozen=True)
class Resumen:
    ciudadanos_por_estado: dict[str, int]
    documentos_por_estado: dict[str, int]
    transferencias_por_estado: dict[str, int]
    outbox_pendientes: int
    outbox_fallidas: int


async def obtener_resumen() -> Resumen:
    async with SessionLocal() as session:
        r1 = await session.execute(select(Ciudadano.estado, func.count()).group_by(Ciudadano.estado))
        ciudadanos = {estado.value: total for estado, total in r1.all()}

        r2 = await session.execute(select(Documento.estado, func.count()).group_by(Documento.estado))
        documentos = {estado.value: total for estado, total in r2.all()}

        r3 = await session.execute(select(Transferencia.estado, func.count()).group_by(Transferencia.estado))
        transferencias = {estado.value: total for estado, total in r3.all()}

        # "Pendientes" agrupa PENDIENTE y EN_PROCESO: el pedido solo distinguia
        # pendientes de fallidas, no las tres categorias por separado.
        r4 = await session.execute(
            select(func.count()).where(Outbox.estado.in_((EstadoOutbox.PENDIENTE, EstadoOutbox.EN_PROCESO)))
        )
        pendientes = r4.scalar_one()

        r5 = await session.execute(select(func.count()).where(Outbox.estado == EstadoOutbox.FALLIDO))
        fallidas = r5.scalar_one()

    return Resumen(
        ciudadanos_por_estado=ciudadanos,
        documentos_por_estado=documentos,
        transferencias_por_estado=transferencias,
        outbox_pendientes=pendientes,
        outbox_fallidas=fallidas,
    )


# --- 2. ciudadanos ---------------------------------------------------------------

# Valores aceptados por el filtro de origen (ver `listar_ciudadanos`). "DESCONOCIDO" no
# es un valor de `OrigenCiudadano`: representa `origen IS NULL`, el ciudadano que ya
# existia antes de que esta columna se agregara -- ver el comentario de ese campo en
# app.models.Ciudadano.
FILTRO_ORIGEN_DESCONOCIDO = "DESCONOCIDO"


def _origen_mostrar(ciudadano: Ciudadano) -> str:
    """Texto honesto sobre como llego el ciudadano (ver docs/especificacion.md,
    "Interoperabilidad entre operadores"): el formato de transferencia acordado entre
    operadores no incluye un identificador del operador de origen, asi que
    `origen_operador_nombre` es una DEDUCCION, nunca un dato recibido. Si se pudo
    resolver contra el directorio en el momento de la recepcion, se muestra ese nombre;
    si no, se muestra el host de `origen_confirm_api` tal cual -- nunca se presenta una
    deduccion como si fuera un dato certificado, y nunca se inventa un nombre para un
    host que no se pudo resolver."""
    if ciudadano.origen is None:
        return "Desconocido"
    if ciudadano.origen == OrigenCiudadano.REGISTRO_DIRECTO:
        return "Registro directo"
    # TRANSFERENCIA
    if ciudadano.origen_operador_nombre:
        return f"Transferido desde {ciudadano.origen_operador_nombre}"
    host = urlparse(ciudadano.origen_confirm_api).hostname if ciudadano.origen_confirm_api else None
    if host:
        return f"Transferido desde {host} (sin resolver en el directorio)"
    return "Transferido (origen sin identificar)"


@dataclass(frozen=True)
class FilaCiudadano:
    id: int
    nombre: str
    estado: str
    creado_en: datetime
    documentos: int
    usado_bytes: int
    origen: str
    # Solo para quien ya se fue (estado TRASLADADO): el operador destino que NOSOTROS
    # elegimos al enviarlo (Transferencia.operador_destino_id), nunca una deduccion --
    # a diferencia de `origen`, este dato si es cierto. None si no se fue, o si por
    # alguna razon no queda una Transferencia CONFIRMADA asociada.
    operador_destino: str | None


async def listar_ciudadanos(
    *, cedula: str | None, nombre: str | None, origen: str | None, page: int
) -> tuple[list[FilaCiudadano], int, int]:
    page = max(page, 1)
    cedula = normalizar_filtro_texto(cedula)
    nombre = normalizar_filtro_texto(nombre)
    condiciones = []
    if cedula:
        # BigInteger, no admite ILIKE directo: se compara como texto para permitir
        # busqueda por coincidencia parcial (como el resto de filtros del portal). Es
        # solo digitos, asi que no distinguir mayusculas no aplica aqui -- LIKE basta.
        condiciones.append(cast(Ciudadano.id, String).like(patron_contiene(cedula), escape=ESCAPE))
    if nombre:
        # Campo aparte de cedula, no un solo cuadro combinado: son dos identificadores
        # de naturaleza distinta (uno numerico, uno de texto libre) y la pantalla de
        # Auditoria ya usa el mismo patron de campos separados por atributo -- mezclar
        # "busca en cedula o nombre" en un solo campo obligaria a adivinar cual de los
        # dos se quiso buscar cuando el texto pudiera ser ambiguo. Sin distinguir
        # acentos (columna_sin_acento_contiene): un nombre real puede escribirse con o
        # sin tilde segun quien lo escriba.
        condiciones.append(columna_sin_acento_contiene(Ciudadano.nombre, nombre))
    if origen == FILTRO_ORIGEN_DESCONOCIDO:
        condiciones.append(Ciudadano.origen.is_(None))
    elif origen in (OrigenCiudadano.REGISTRO_DIRECTO.value, OrigenCiudadano.TRANSFERENCIA.value):
        condiciones.append(Ciudadano.origen == OrigenCiudadano(origen))

    async with SessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(Ciudadano).where(*condiciones))).scalar_one()
        resultado = await session.execute(
            select(Ciudadano)
            .where(*condiciones)
            .order_by(Ciudadano.creado_en.desc())
            .offset((page - 1) * TAMANO_PAGINA)
            .limit(TAMANO_PAGINA)
        )
        pagina_ciudadanos = list(resultado.scalars().all())
        ids = [c.id for c in pagina_ciudadanos]
        ids_trasladados = [c.id for c in pagina_ciudadanos if c.estado.value == "TRASLADADO"]

        conteo_documentos: dict[int, int] = {}
        bytes_usados: dict[int, int] = {}
        if ids:
            rd = await session.execute(
                select(Documento.ciudadano_id, func.count())
                .where(Documento.ciudadano_id.in_(ids))
                .group_by(Documento.ciudadano_id)
            )
            conteo_documentos = dict(rd.all())

            # Mismo calculo de cuota que el resto de la aplicacion (ver
            # app.identidad.perfil_servicios._usado_bytes): solo temporal y ACTIVO.
            rb = await session.execute(
                select(Documento.ciudadano_id, func.coalesce(func.sum(Documento.tamano_bytes), 0))
                .where(
                    Documento.ciudadano_id.in_(ids),
                    Documento.certificado.is_(False),
                    Documento.estado == EstadoDocumento.ACTIVO,
                )
                .group_by(Documento.ciudadano_id)
            )
            bytes_usados = dict(rb.all())

        destinos: dict[int, str] = {}
        if ids_trasladados:
            # Certero, no deducido (a diferencia de `origen`): el operador destino de un
            # traslado saliente lo elegimos nosotros mismos al enviarlo (CU-03). Toma la
            # confirmacion mas reciente si por alguna razon hubiera mas de una fila.
            rt = await session.execute(
                select(Transferencia.ciudadano_id, OperadorCache.nombre)
                .join(OperadorCache, OperadorCache.id == Transferencia.operador_destino_id)
                .where(
                    Transferencia.ciudadano_id.in_(ids_trasladados),
                    Transferencia.estado == EstadoTransferencia.CONFIRMADA,
                )
                .order_by(Transferencia.confirmada_en.desc())
            )
            for ciudadano_id, nombre_operador in rt.all():
                destinos.setdefault(ciudadano_id, nombre_operador)

    filas = [
        FilaCiudadano(
            id=c.id,
            nombre=c.nombre,
            estado=c.estado.value,
            creado_en=c.creado_en,
            documentos=conteo_documentos.get(c.id, 0),
            usado_bytes=bytes_usados.get(c.id, 0),
            origen=_origen_mostrar(c),
            operador_destino=destinos.get(c.id),
        )
        for c in pagina_ciudadanos
    ]
    return filas, total, page


# --- 3. transferencias, entrantes y salientes en una sola vista ---------------------


@dataclass(frozen=True)
class FilaTransferencia:
    direccion: str  # "SALIENTE" | "ENTRANTE"
    operador: str
    estado: str
    fecha_envio: datetime
    fecha_confirmacion: datetime | None
    purgar_despues_de: datetime | None


_ESTADO_ENTRANTE = {
    EstadoOutbox.FALLIDO: "RECHAZADA",
    EstadoOutbox.COMPLETADO: "RECIBIDA",
}


def _mostrar_operador_entrante(confirm_api: str | None, operadores: list[OperadorCache]) -> str:
    """CU-16 no persiste la identidad del operador de origen en ninguna tabla propia
    (solo queda en el `confirm_api` que trae la transferencia, dentro del payload de
    `Outbox`): se resuelve por coincidencia de host contra el directorio con
    `app.interoperabilidad.transferencias.resolver_operador_por_host` -- la misma
    funcion que usa `app.interoperabilidad.outbox` para guardar el origen del
    ciudadano al recibirlo (`Ciudadano.origen_operador_id`), para que las dos lecturas
    del mismo dato nunca puedan divergir. Esta funcion solo le agrega el formato de
    texto que necesita esta pantalla: nunca presenta la deduccion como una identidad
    confirmada -- el ecosistema no tiene autenticacion real entre operadores
    (CLAUDE.md, "trampa 6")."""
    operador = resolver_operador_por_host(operadores, confirm_api)
    if operador is not None:
        return operador.nombre
    host = urlparse(confirm_api).hostname if confirm_api else None
    return f"Desconocido ({host})" if host else "Desconocido"


async def listar_transferencias(*, page: int) -> tuple[list[FilaTransferencia], int]:
    """Une dos fuentes de datos de forma muy distinta entre si -- la tabla
    `transferencia` (solo transferencias salientes, CU-03) y las filas de `outbox` con
    operacion `receiveTransferCitizen` (la unica huella de una transferencia entrante,
    CU-16, que no tiene tabla propia) -- por eso la union, el orden y la paginacion se
    resuelven en Python despues de traer ambas listas completas, en vez de una sola
    consulta SQL. No se toco ningun modelo ni la logica de negocio de CU-03/CU-16 para
    esto: es una lectura por encima de lo que ya existe."""
    page = max(page, 1)
    async with SessionLocal() as session:
        operadores = list((await session.execute(select(OperadorCache))).scalars().all())
        nombres_operadores = {op.id: op.nombre for op in operadores}

        salientes = list(
            (await session.execute(select(Transferencia).order_by(Transferencia.enviada_en.desc()))).scalars().all()
        )
        entrantes = list(
            (
                await session.execute(
                    select(Outbox)
                    .where(Outbox.operacion == "receiveTransferCitizen")
                    .order_by(Outbox.creado_en.desc())
                )
            )
            .scalars()
            .all()
        )

    filas: list[FilaTransferencia] = []
    for t in salientes:
        filas.append(
            FilaTransferencia(
                direccion="SALIENTE",
                operador=nombres_operadores.get(t.operador_destino_id, t.operador_destino_id),
                estado=t.estado.value,
                fecha_envio=t.enviada_en,
                fecha_confirmacion=t.confirmada_en,
                purgar_despues_de=t.purgar_despues_de,
            )
        )
    for o in entrantes:
        confirm_api = (o.payload or {}).get("confirm_api")
        filas.append(
            FilaTransferencia(
                direccion="ENTRANTE",
                operador=_mostrar_operador_entrante(confirm_api, operadores),
                estado=_ESTADO_ENTRANTE.get(o.estado, "EN_PROCESO"),
                fecha_envio=o.creado_en,
                fecha_confirmacion=None,
                purgar_despues_de=None,
            )
        )

    filas.sort(key=lambda f: f.fecha_envio, reverse=True)
    total = len(filas)
    inicio = (page - 1) * TAMANO_PAGINA
    return filas[inicio : inicio + TAMANO_PAGINA], total


# --- 4. bandeja de salida ------------------------------------------------------------


@dataclass(frozen=True)
class FilaOutbox:
    id: int
    operacion: str
    estado: str
    intentos: int
    ultimo_error: str | None
    proximo_intento: datetime | None
    creado_en: datetime


async def listar_outbox(*, page: int) -> tuple[list[FilaOutbox], int]:
    page = max(page, 1)
    async with SessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(Outbox))).scalar_one()
        resultado = await session.execute(
            select(Outbox).order_by(Outbox.creado_en.desc()).offset((page - 1) * TAMANO_PAGINA).limit(TAMANO_PAGINA)
        )
        pagina_outbox = list(resultado.scalars().all())

    filas = [
        FilaOutbox(
            id=o.id,
            operacion=o.operacion,
            estado=o.estado.value,
            intentos=o.intentos,
            ultimo_error=o.ultimo_error,
            proximo_intento=o.proximo_intento,
            creado_en=o.creado_en,
        )
        for o in pagina_outbox
    ]
    return filas, total


# --- 5. auditoria ------------------------------------------------------------------


@dataclass(frozen=True)
class FilaAuditoria:
    id: int
    momento: datetime
    actor: str
    accion: str
    recurso: str | None
    correlation_id: str | None


async def listar_auditoria(
    *, cedula: str | None, accion: str | None, desde: datetime | None, hasta: datetime | None, page: int
) -> tuple[list[FilaAuditoria], int]:
    """Deliberadamente no devuelve `Auditoria.detalle`: auditar cada punto del proyecto
    que escribe ahi para garantizar que ninguno registra algo sensible es mas riesgoso
    que simplemente no mostrarlo nunca en una consola de solo lectura -- ver la AD
    nueva en docs/especificacion.md."""
    page = max(page, 1)
    cedula = normalizar_filtro_texto(cedula)
    accion = normalizar_filtro_texto(accion)
    condiciones = []
    if cedula:
        condiciones.append(Auditoria.recurso.ilike(patron_contiene(cedula), escape=ESCAPE))
    if accion:
        condiciones.append(Auditoria.accion.ilike(patron_contiene(accion), escape=ESCAPE))
    if desde:
        condiciones.append(Auditoria.momento >= desde)
    if hasta:
        condiciones.append(Auditoria.momento <= hasta)

    async with SessionLocal() as session:
        total = (
            await session.execute(select(func.count()).select_from(Auditoria).where(*condiciones))
        ).scalar_one()
        resultado = await session.execute(
            select(Auditoria)
            .where(*condiciones)
            .order_by(Auditoria.momento.desc())
            .offset((page - 1) * TAMANO_PAGINA)
            .limit(TAMANO_PAGINA)
        )
        pagina_auditoria = list(resultado.scalars().all())

    filas = [
        FilaAuditoria(
            id=a.id,
            momento=a.momento,
            actor=a.actor,
            accion=a.accion,
            recurso=a.recurso,
            correlation_id=a.correlation_id,
        )
        for a in pagina_auditoria
    ]
    return filas, total
