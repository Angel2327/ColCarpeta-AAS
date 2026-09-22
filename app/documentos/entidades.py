"""CU-13 (mínimo): depósito de un documento certificado por una entidad emisora en la
carpeta de un ciudadano ya afiliado a ColCarpeta.

Esta es la primera vía real por la que un documento llega a `certificado = true`: ni la
carga propia del ciudadano (CU-05) ni la sustitución (CU-10) lo permiten, así que hasta
ahora esa condición nunca se ejercitaba de verdad (ver CLAUDE.md, "Pendiente"). Con esto,
las reglas que dependen de ella —no consumir cuota y no admitir borrado a solicitud del
ciudadano (CU-08)— tienen por fin un documento real que las active.

CU-04 completo (registro público de entidades, RF9) no está implementado: lo único que
existe es la tabla `entidad_emisora` y su credencial. No hay ninguna ruta pública que
cree una entidad — la única forma de darla de alta es `scripts/alta_entidad_emisora.py`,
que exige acceso directo a la base de datos, fuera de la API.

CU-09 (validación de firma digital) sigue sin implementar: `firma_valida` queda en NULL
para lo que llega por esta vía, igual que en CU-05. Este es el punto donde encajaría esa
validación cuando exista pyHanko — no se inventa aquí un resultado falso.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from app.config import get_config
from app.db import SessionLocal
from app.documentos.almacenamiento import FalloAlmacenamiento, eliminar_objeto, generar_clave, subir_objeto
from app.documentos.router import resolver_sustitucion
from app.documentos.tipos import TIPOS_PERMITIDOS, detectar_content_type
from app.errors import ErrorDeNegocio
from app.identidad.token_acceso import hash_token
from app.models import Auditoria, Ciudadano, Documento, EntidadEmisora, EstadoCiudadano, EstadoDocumento, EstadoEntidadEmisora

router = APIRouter(prefix="/api/v1/entidades", tags=["entidades emisoras"])


class RespuestaDocumentoDepositado(BaseModel):
    id: uuid.UUID
    titulo: str
    tipo: str
    entidad_emisora: str
    fecha_emision: date | None
    certificado: bool
    firma_valida: bool | None
    sustituye_a: uuid.UUID | None
    tamano_bytes: int
    creado_en: datetime


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


async def entidad_actual(x_api_key: str | None = Header(default=None, alias="X-Api-Key")) -> EntidadEmisora:
    """Resuelve la entidad emisora por su clave de API, buscada por igualdad de hash
    (mismo esquema que el token de primer acceso) -- no hace falta un identificador de
    entidad aparte en cada petición.

    Una entidad `REVOCADA` (scripts/alta_entidad_emisora.py revocar) deja de poder
    autenticarse de inmediato, con la misma clave que tenía antes: revocar no la
    invalida, solo bloquea su uso hasta que se reactive."""
    if not x_api_key:
        raise ErrorDeNegocio("NO_AUTENTICADO", "Se requiere la clave de API de la entidad, en el encabezado X-Api-Key.")
    async with SessionLocal() as session:
        resultado = await session.execute(select(EntidadEmisora).where(EntidadEmisora.api_key_hash == hash_token(x_api_key)))
        entidad = resultado.scalar_one_or_none()
    if entidad is None:
        raise ErrorDeNegocio("NO_AUTENTICADO", "La clave de API no es valida.")
    if entidad.estado != EstadoEntidadEmisora.ACTIVA:
        raise ErrorDeNegocio("NO_AUTENTICADO", "La entidad emisora fue revocada.")
    return entidad


@router.post("/documentos", response_model=RespuestaDocumentoDepositado, status_code=201)
async def depositar_documento(
    request: Request,
    cedula: int = Form(..., gt=0),
    archivo: UploadFile = File(...),
    titulo: str = Form(..., min_length=1, max_length=255),
    tipo: str = Form(..., min_length=1, max_length=100),
    fecha_emision: date | None = Form(None),
    sustituye_a: uuid.UUID | None = Form(None),
    entidad: EntidadEmisora = Depends(entidad_actual),
) -> RespuestaDocumentoDepositado:
    """Deposita un documento certificado en la carpeta de un ciudadano (CU-13).

    Requiere autenticarse como entidad emisora con la clave de API en el encabezado
    `X-Api-Key`. El documento queda marcado como certificado y con la entidad emisora
    tomada de la credencial usada, nunca de un valor declarado en la petición; no
    consume la cuota de almacenamiento del ciudadano ni admite luego un borrado a su
    solicitud (CU-08). Si `sustituye_a` incluye el id de un documento temporal propio
    del ciudadano, ese documento pasa a reemplazado, igual que en la carga propia
    (CU-10), sin perder su historia.

    Devuelve 401 si la clave de API falta o es inválida, 404 si la cédula no
    corresponde a un ciudadano afiliado y activo en ColCarpeta, 413 si el archivo
    excede el tamaño máximo, o 415 si el tipo de archivo no está permitido.
    """
    cfg = get_config()
    contenido = await archivo.read()

    if len(contenido) > cfg.tamano_maximo_archivo_bytes:
        raise ErrorDeNegocio(
            "ARCHIVO_DEMASIADO_GRANDE",
            "El archivo excede el tamano maximo permitido.",
            detalle={"limite_bytes": cfg.tamano_maximo_archivo_bytes},
        )

    content_type = detectar_content_type(contenido)
    if content_type is None:
        raise ErrorDeNegocio(
            "TIPO_NO_PERMITIDO",
            "El tipo de archivo no esta permitido.",
            detalle={"tipos_permitidos": list(TIPOS_PERMITIDOS)},
        )

    hash_sha256 = hashlib.sha256(contenido).hexdigest()
    fecha_emision_dt = datetime.combine(fecha_emision, time.min, tzinfo=timezone.utc) if fecha_emision else None

    async with SessionLocal() as session:
        ciudadano = await session.get(Ciudadano, cedula)
        if ciudadano is None or ciudadano.estado != EstadoCiudadano.ACTIVO:
            raise ErrorDeNegocio("RECURSO_NO_ENCONTRADO", "El ciudadano no esta afiliado a ColCarpeta.")

        anterior = await resolver_sustitucion(session, ciudadano_id=cedula, sustituye_a=sustituye_a)

        clave = generar_clave(content_type)
        subir_objeto(clave=clave, contenido=contenido, content_type=content_type)

        nuevo_id = uuid.uuid4()
        nuevo = Documento(
            id=nuevo_id,
            ciudadano_id=cedula,
            titulo=titulo,
            tipo=tipo,
            entidad_emisora=entidad.nombre,
            fecha_emision=fecha_emision_dt,
            s3_key=clave,
            content_type=content_type,
            tamano_bytes=len(contenido),
            hash_sha256=hash_sha256,
            certificado=True,
            firma_valida=None,
            sustituye_a_id=anterior.id if anterior is not None else None,
        )
        session.add(nuevo)

        if anterior is not None:
            anterior.estado = EstadoDocumento.REEMPLAZADO

        from app.notificaciones.correo import enviar_correo

        await enviar_correo(
            session,
            ciudadano_id=cedula,
            destinatario=ciudadano.email_personal,
            asunto=f"Nuevo documento certificado de {entidad.nombre}",
            cuerpo=(
                f"Hola {ciudadano.nombre},\n\n"
                f"{entidad.nombre} deposito el documento \"{titulo}\" en tu carpeta de ColCarpeta. "
                "Ya esta certificado y disponible en tu carpeta.\n"
            ),
        )

        session.add(
            Auditoria(
                actor=f"entidad:{entidad.id}",
                accion="documento.depositado_por_entidad" if anterior is None else "documento.sustituido_por_entidad",
                recurso=str(nuevo_id),
                ciudadano_id=cedula,
                correlation_id=_correlation_id(request),
                detalle={
                    "entidad_id": entidad.id,
                    "entidad_nombre": entidad.nombre,
                    "documento_anterior_id": str(anterior.id) if anterior is not None else None,
                },
            )
        )

        try:
            await session.commit()
        except Exception:
            with contextlib.suppress(FalloAlmacenamiento):
                eliminar_objeto(clave=clave)
            raise

        await session.refresh(nuevo)

    return RespuestaDocumentoDepositado(
        id=nuevo.id,
        titulo=nuevo.titulo,
        tipo=nuevo.tipo,
        entidad_emisora=nuevo.entidad_emisora,
        fecha_emision=nuevo.fecha_emision.date() if nuevo.fecha_emision else None,
        certificado=nuevo.certificado,
        firma_valida=nuevo.firma_valida,
        sustituye_a=nuevo.sustituye_a_id,
        tamano_bytes=nuevo.tamano_bytes,
        creado_en=nuevo.creado_en,
    )
