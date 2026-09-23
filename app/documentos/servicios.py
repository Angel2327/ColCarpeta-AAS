"""Capa de servicios de Documentos: la lógica de negocio de CU-05/06/07/08/09/10/11
sobre documentos propios del ciudadano, compartida entre las rutas JSON de la API
(`app.documentos.router`) y las pantallas HTML del portal (`app.portal`). Ninguna de
las dos capas de transporte duplica esta lógica -- ambas la piden aquí. Ver AD-11
(docs/especificacion.md) para por qué el portal no llama a esta API por HTTP en vez de
compartir estas funciones.

Las funciones devuelven instancias de `Documento` ya refrescadas (nunca expiradas) antes
de que su sesión se cierre, para que quien llama pueda leer sus columnas después sin un
`DetachedInstanceError` -- el mismo patrón que ya usaba `cargar_documento` en la ruta
original. No devuelven los modelos `Respuesta*` de la API (esos son responsabilidad de
`app.documentos.router`, que sí conoce el contrato JSON); el portal construye su propio
HTML a partir del mismo `Documento`.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db import SessionLocal
from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto, generar_clave, generar_url_descarga, subir_objeto
from app.documentos.tipos import TIPOS_PERMITIDOS, detectar_content_type
from app.errors import ErrorDeNegocio
from app.models import Auditoria, Ciudadano, Documento, EstadoAutenticacionDocumento, EstadoCiudadano, EstadoDocumento, Outbox

TAMANO_PAGINA_DEFECTO = 20
TAMANO_PAGINA_MAXIMO = 100


def _exigir_activo(ciudadano: Ciudadano) -> None:
    if ciudadano.estado != EstadoCiudadano.ACTIVO:
        raise ErrorDeNegocio("ESTADO_INVALIDO", "Tu carpeta no esta activa.")


def _a_datetime_utc(d: date, *, fin_del_dia: bool = False) -> datetime:
    hora = time.max if fin_del_dia else time.min
    return datetime.combine(d, hora, tzinfo=timezone.utc)


async def resolver_sustitucion(
    session: AsyncSession, *, ciudadano_id: int, sustituye_a: uuid.UUID | None
) -> Documento | None:
    """CU-10: valida y devuelve el documento que una nueva carga sustituye, o None si
    `sustituye_a` no vino. Compartido entre la carga propia del ciudadano
    (`cargar_documento`, ruta JSON y portal) y el depósito de una entidad emisora
    (`app.documentos.entidades`, CU-13), que también puede certificar el reemplazo de
    un documento temporal existente."""
    if sustituye_a is None:
        return None
    anterior = await session.get(Documento, sustituye_a)
    if anterior is None or anterior.estado != EstadoDocumento.ACTIVO:
        raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "El documento a sustituir no existe.")
    if anterior.ciudadano_id != ciudadano_id:
        raise ErrorDeNegocio("NO_AUTORIZADO", "El documento a sustituir no pertenece a esa carpeta.")
    if anterior.certificado:
        raise ErrorDeNegocio("ESTADO_INVALIDO", "No se puede sustituir un documento certificado.")
    return anterior


async def cargar_documento(
    *,
    ciudadano_id: int,
    contenido: bytes,
    titulo: str,
    tipo: str,
    entidad_emisora: str | None,
    fecha_emision: date | None,
    sustituye_a: uuid.UUID | None,
    correlation_id: str | None,
) -> tuple[Documento, bool]:
    """CU-05: valida, sube al bucket y crea el registro del documento.

    Devuelve `(documento, fue_creado)`: `fue_creado=False` cuando el archivo ya se
    había cargado antes (mismo `hash_sha256` entre lo vigente) y se devuelve el
    documento existente sin duplicar nada -- E5. Ver el docstring de
    `app.documentos.router.cargar_documento` para el detalle completo de cada rama
    (A1/A2, E1-E5); este es ese mismo cuerpo, movido para que el portal lo comparta.
    """
    cfg = get_config()

    # --- E3: tamano sobre el limite -------------------------------------------------
    if len(contenido) > cfg.tamano_maximo_archivo_bytes:
        raise ErrorDeNegocio(
            "ARCHIVO_DEMASIADO_GRANDE",
            "El archivo excede el tamano maximo permitido.",
            detalle={"limite_bytes": cfg.tamano_maximo_archivo_bytes},
        )

    # --- E2: tipo no permitido (por contenido, no por extension) --------------------
    content_type = detectar_content_type(contenido)
    if content_type is None:
        raise ErrorDeNegocio(
            "TIPO_NO_PERMITIDO",
            "El tipo de archivo no esta permitido.",
            detalle={"tipos_permitidos": list(TIPOS_PERMITIDOS)},
        )

    hash_sha256 = hashlib.sha256(contenido).hexdigest()

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, ciudadano_id)
        assert ciudadano is not None
        _exigir_activo(ciudadano)

        # --- E5: archivo duplicado segun hash_sha256 (solo entre lo vigente) --------
        r = await session.execute(
            select(Documento).where(
                Documento.ciudadano_id == ciudadano.id,
                Documento.hash_sha256 == hash_sha256,
                Documento.estado == EstadoDocumento.ACTIVO,
            )
        )
        duplicado = r.scalar_one_or_none()
        if duplicado is not None:
            return duplicado, False

        # --- A2/CU-10: sustituye a un documento temporal, solo si el ciudadano lo pide
        anterior = await resolver_sustitucion(session, ciudadano_id=ciudadano.id, sustituye_a=sustituye_a)

        # --- E1: cuota agotada (solo temporales activos; certificados no consumen
        # cuota, y lo reemplazado/eliminado ya no cuenta) ----------------------------
        r = await session.execute(
            select(func.coalesce(func.sum(Documento.tamano_bytes), 0))
            .select_from(Documento)
            .where(
                Documento.ciudadano_id == ciudadano.id,
                Documento.certificado.is_(False),
                Documento.estado == EstadoDocumento.ACTIVO,
            )
        )
        # SUM(bigint) en Postgres devuelve NUMERIC -> Decimal; hay que volverlo int.
        usado = int(r.scalar_one())
        if anterior is not None:
            # Todavia ACTIVO en este punto (se marca REEMPLAZADO mas abajo, tras superar
            # esta validacion): la consulta de arriba ya lo conto, hay que descontarlo.
            usado -= anterior.tamano_bytes
        if usado + len(contenido) > cfg.cuota_ciudadano_bytes:
            raise ErrorDeNegocio(
                "CUOTA_AGOTADA",
                "La cuota de documentos temporales esta agotada.",
                detalle={"cuota_bytes": cfg.cuota_ciudadano_bytes, "usado_bytes": usado},
            )

        # --- E4: falla del almacenamiento => nada se persiste (atomico) -------------
        clave = generar_clave(content_type)
        subir_objeto(clave=clave, contenido=contenido, content_type=content_type)

        nuevo_id = uuid.uuid4()
        nuevo = Documento(
            id=nuevo_id,
            ciudadano_id=ciudadano.id,
            titulo=titulo,
            tipo=tipo,
            entidad_emisora=entidad_emisora,
            fecha_emision=_a_datetime_utc(fecha_emision) if fecha_emision else None,
            s3_key=clave,
            content_type=content_type,
            tamano_bytes=len(contenido),
            hash_sha256=hash_sha256,
            certificado=False,
            firma_valida=None,
            sustituye_a_id=anterior.id if anterior is not None else None,
        )
        session.add(nuevo)

        # CU-10: el anterior no se borra -- pasa a REEMPLAZADO, fila y objeto se
        # conservan como historia.
        if anterior is not None:
            anterior.estado = EstadoDocumento.REEMPLAZADO

        # A1/CU-09: solo un PDF puede traer una firma PAdES que valga la pena revisar.
        # La validacion misma corre en segundo plano (AD-05).
        if content_type == "application/pdf":
            session.add(
                Outbox(
                    operacion="validarFirma",
                    payload={"documento_id": str(nuevo_id), "correlation_id": correlation_id},
                )
            )

        session.add(
            Auditoria(
                actor=str(ciudadano.id),
                accion="documento.sustituido" if anterior is not None else "documento.cargado",
                recurso=str(nuevo_id),
                ciudadano_id=ciudadano.id,
                correlation_id=correlation_id,
                detalle={"documento_anterior_id": str(anterior.id) if anterior is not None else None},
            )
        )

        try:
            await session.commit()
        except Exception:
            with contextlib.suppress(FalloAlmacenamiento):
                eliminar_objeto(clave=clave)
            raise

        await session.refresh(nuevo)

    return nuevo, True


async def listar_documentos(
    *,
    ciudadano_id: int,
    tipo: str | None,
    entidad: str | None,
    desde: date | None,
    hasta: date | None,
    certificado: bool | None,
    estado_autenticacion: EstadoAutenticacionDocumento | None,
    q: str | None,
    page: int,
    size: int,
) -> tuple[list[Documento], int, int, int]:
    """CU-06/CU-07: busca y clasifica los documentos vigentes del ciudadano.

    Devuelve `(items, total, page, size)` -- `page`/`size` ya normalizados (página
    mínima 1, tamaño entre 1 y `TAMANO_PAGINA_MAXIMO`).
    """
    page = max(page, 1)
    size = max(1, min(size, TAMANO_PAGINA_MAXIMO))

    condiciones = [Documento.ciudadano_id == ciudadano_id, Documento.estado == EstadoDocumento.ACTIVO]
    if tipo:
        condiciones.append(Documento.tipo == tipo)
    if entidad:
        condiciones.append(Documento.entidad_emisora.ilike(f"%{entidad}%"))
    if desde:
        condiciones.append(Documento.fecha_emision >= _a_datetime_utc(desde))
    if hasta:
        condiciones.append(Documento.fecha_emision <= _a_datetime_utc(hasta, fin_del_dia=True))
    if certificado is not None:
        condiciones.append(Documento.certificado.is_(certificado))
    if estado_autenticacion is not None:
        condiciones.append(Documento.estado_autenticacion == estado_autenticacion)
    if q:
        condiciones.append(Documento.titulo.ilike(f"%{q}%"))

    async with SessionLocal() as session:
        total = (
            await session.execute(select(func.count()).select_from(Documento).where(*condiciones))
        ).scalar_one()
        resultado = await session.execute(
            select(Documento)
            .where(*condiciones)
            .order_by(Documento.creado_en.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = list(resultado.scalars().all())

    return items, total, page, size


async def obtener_documento_propio(session: AsyncSession, documento_id: uuid.UUID, ciudadano_id: int) -> Documento:
    """Devuelve un documento propio siempre que siga siendo accesible: ACTIVO o
    REEMPLAZADO (CU-10 lo conserva como historia consultable). Uno ELIMINADO (CU-08) se
    trata igual que si no existiera -- "dejan de ser accesibles" (especificación,
    "Borrado")."""
    documento = await session.get(Documento, documento_id)
    if documento is None or documento.estado == EstadoDocumento.ELIMINADO:
        raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "El documento no existe.")
    if documento.ciudadano_id != ciudadano_id:
        raise ErrorDeNegocio("NO_AUTORIZADO", "El documento no pertenece a tu carpeta.")
    return documento


async def obtener_documento(*, ciudadano_id: int, documento_id: uuid.UUID) -> Documento:
    async with SessionLocal() as session:
        return await obtener_documento_propio(session, documento_id, ciudadano_id)


async def eliminar_documento(*, ciudadano_id: int, documento_id: uuid.UUID, correlation_id: str | None) -> None:
    """CU-08: borrado diferido de un documento no certificado.

    Ver `app.documentos.router.eliminar_documento` para el detalle de cada rechazo.
    """
    cfg = get_config()
    async with SessionLocal() as session:
        documento = await obtener_documento_propio(session, documento_id, ciudadano_id)

        if documento.certificado:
            raise ErrorDeNegocio("DOCUMENTO_CERTIFICADO", "No se puede eliminar un documento certificado.")
        if documento.estado != EstadoDocumento.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El documento ya fue reemplazado por una version mas reciente.")

        documento.estado = EstadoDocumento.ELIMINADO
        documento.purgar_despues_de = datetime.now(timezone.utc) + timedelta(days=cfg.purge_delay_days)

        session.add(
            Auditoria(
                actor=str(ciudadano_id),
                accion="documento.eliminado",
                recurso=str(documento.id),
                ciudadano_id=ciudadano_id,
                correlation_id=correlation_id,
                detalle={"purgar_despues_de": documento.purgar_despues_de.isoformat()},
            )
        )
        await session.commit()


async def generar_descarga(*, ciudadano_id: int, documento_id: uuid.UUID, correlation_id: str | None) -> tuple[str, datetime]:
    """Genera un enlace temporal de descarga para un documento propio. Devuelve
    `(url, expira_en)`."""
    cfg = get_config()
    async with SessionLocal() as session:
        documento = await obtener_documento_propio(session, documento_id, ciudadano_id)

        url = generar_url_descarga(clave=documento.s3_key, ttl_segundos=cfg.presigned_url_ttl_descarga)
        expira_en = datetime.now(timezone.utc) + timedelta(seconds=cfg.presigned_url_ttl_descarga)

        # "Cada generacion de un enlace firmado se registra en auditoria con el
        # documento, el destino y el momento" (Seguridad y manejo de documentos).
        session.add(
            Auditoria(
                actor=str(ciudadano_id),
                accion="documento.enlace_generado",
                recurso=str(documento.id),
                ciudadano_id=ciudadano_id,
                correlation_id=correlation_id,
                detalle={"destino": "ciudadano", "expira_en": expira_en.isoformat()},
            )
        )
        await session.commit()

    return url, expira_en


async def solicitar_autenticacion(
    *, ciudadano_id: int, documento_id: uuid.UUID, correlation_id: str | None
) -> tuple[Documento, int]:
    """CU-11: solicita la autenticación de un documento propio ante GovCarpeta.

    Devuelve `(documento, status_code)`: 200 si ya estaba `AUTENTICADO` (A1, se
    muestra el resultado guardado sin reenviar), 202 si se encoló una solicitud nueva o
    ya había una en curso (idempotente).
    """
    async with SessionLocal() as session:
        documento = await obtener_documento_propio(session, documento_id, ciudadano_id)

        # No tiene sentido pedirle al centralizador que autentique un documento que el
        # propio ciudadano ya sustituyo (CU-10): un REEMPLAZADO sigue siendo consultable
        # para ver su historia, pero esto es una operacion nueva sobre el, no una
        # lectura -- se rechaza sin importar si ya tenia un resultado guardado de antes.
        if documento.estado != EstadoDocumento.ACTIVO:
            raise ErrorDeNegocio("ESTADO_INVALIDO", "El documento ya no esta vigente: fue reemplazado por una version mas reciente.")

        # A1: ya autenticado, no se reenvia; se muestra el resultado guardado.
        if documento.estado_autenticacion == EstadoAutenticacionDocumento.AUTENTICADO:
            return documento, 200

        # Idempotencia: ya hay una solicitud en curso, no se duplica la entrada de outbox.
        if documento.estado_autenticacion == EstadoAutenticacionDocumento.PENDIENTE:
            return documento, 202

        documento.estado_autenticacion = EstadoAutenticacionDocumento.PENDIENTE
        documento.autenticacion_actualizada_en = datetime.now(timezone.utc)

        session.add(
            Outbox(
                operacion="authenticateDocument",
                payload={
                    "documento_id": str(documento.id),
                    "cedula": ciudadano_id,
                    "titulo": documento.titulo,
                    "correlation_id": correlation_id,
                },
            )
        )
        session.add(
            Auditoria(
                actor=str(ciudadano_id),
                accion="documento.autenticacion_solicitada",
                recurso=str(documento.id),
                ciudadano_id=ciudadano_id,
                correlation_id=correlation_id,
                detalle={},
            )
        )
        await session.commit()
        await session.refresh(documento)

    return documento, 202


async def consultar_autenticacion(*, ciudadano_id: int, documento_id: uuid.UUID) -> Documento:
    async with SessionLocal() as session:
        return await obtener_documento_propio(session, documento_id, ciudadano_id)
