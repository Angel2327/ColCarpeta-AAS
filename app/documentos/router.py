"""CU-05: carga de documentos, y las consultas basicas de la carpeta (CU-06/CU-07) --
ruta JSON de la API.

La lógica de negocio vive en `app.documentos.servicios`, compartida con las pantallas
de la carpeta del portal (`app.portal`): esta ruta solo adapta esas llamadas al formato
JSON de la API (lee `UploadFile`/`Form`, traduce a los modelos `Respuesta*` de abajo, y
fija el código de estado según lo que el servicio devuelve). Ver
docs/especificacion.md: "Flujos a implementar" (flujo 3), "Contrato de la API propia"
(documentos), "Flujos alternos y de excepcion" (CU-05, A1-A2, E1-E5), "Parametros y
limites" (cuota, tamano, tipos, paginacion) y "Seguridad y manejo de documentos"
(enlaces firmados, claves de objeto aleatorias). AD-11 explica por qué el portal no
llama a esta ruta por HTTP en vez de compartir la función de servicio.

A1 (firma digital, CU-09): si el archivo es un PDF, se encola `validarFirma` en la misma
transaccion que crea el documento -- la validacion criptografica corre en segundo plano
(`app.interoperabilidad.outbox`, AD-05: consume procesador, no va en la peticion del
ciudadano), nunca contra un archivo `image/jpeg` o `image/png` (no pueden traer una
firma PAdES). `firma_valida` queda en NULL hasta que la bandeja de salida lo resuelva
-- o para siempre, si el PDF no trae ninguna firma embebida. Ver app.documentos.firma
para el detalle de que se valida (y que no: la cadena de confianza contra una autoridad
certificadora real, que este proyecto no tiene).

A2 (sustitucion, CU-10) es explicita, no inferida: el formulario acepta `sustituye_a` con
el id del documento temporal a reemplazar. Sin ese campo, toda carga crea un documento
nuevo, aunque coincidan titulo/tipo/entidad_emisora con uno existente -- inferir la
sustitucion por esos metadatos haria que dos documentos distintos con la misma
descripcion se pisaran entre si y se perdiera el anterior sin que el ciudadano lo pidiera.
Cuando si viene, el documento anterior NO se borra: pasa a `estado=REEMPLAZADO` (fila y
objeto del bucket se conservan, sin perder su historia) y el nuevo queda enlazado a el
por `sustituye_a_id`. Un documento REEMPLAZADO deja de listarse (CU-07) y de contar
contra la cuota, pero sigue siendo consultable por `GET /documentos/{id}` para seguir el
historial hacia atras; solo un documento ELIMINADO (CU-08) deja de ser accesible del
todo.

CU-08 (`DELETE /documentos/{id}`): borrado diferido de un documento no certificado, igual
patron que la purga de `transferencia` -- se marca `ELIMINADO` con `purgar_despues_de` y
`app.interoperabilidad.outbox._purgar_documentos` lo purga fisicamente (fila y objeto)
al cumplirse el plazo.

CU-11 (autenticacion ante GovCarpeta): `POST .../autenticacion` solo escribe en outbox y
marca el documento PENDIENTE; el enlace firmado de 15 min, la llamada al centralizador y
la actualizacion final del estado las hace `app.interoperabilidad.outbox` en segundo
plano (ver ese modulo para E1-E5). A2 ("la solicitud la origina el registro y no el
ciudadano") no tiene todavia un segundo llamador real -- CU-01 no encola la autenticacion
del documento de identidad en esta entrega -- pero el propio proceso de outbox ya actua
siempre como actor "sistema" al aplicar el resultado, sea quien sea quien encolo.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.documentos import servicios
from app.identidad.dependencias import ciudadano_actual
from app.models import Ciudadano, Documento, EstadoAutenticacionDocumento, EstadoDocumento

router = APIRouter(prefix="/api/v1/documentos", tags=["documentos"])


class RespuestaDocumento(BaseModel):
    id: uuid.UUID
    titulo: str
    tipo: str
    entidad_emisora: str | None
    fecha_emision: date | None
    certificado: bool
    # CU-09: sin valor mientras no hay firma que validar (el archivo no es un PDF, el
    # PDF no trae firma embebida, o la validacion todavia no corrio en segundo plano) --
    # eso es distinto de `false`, que es una firma que sí se validó y no pasó. `firmante`
    # y `firma_fecha` son lo que la propia firma declara (nunca verificado contra una
    # autoridad certificadora real: ver app.documentos.firma) y solo tienen valor junto
    # con `firma_valida`. `certificado` es una fuente de procedencia distinta e
    # independiente (CU-13: lo deposité una entidad emisora autenticada, no el
    # ciudadano) -- un documento puede tener cualquier combinación de las dos.
    firma_valida: bool | None = Field(
        description=(
            "Sin valor si el documento no tiene una firma digital que validar, o si la "
            "validación todavía no terminó. `false` significa que sí se validó una firma "
            "y no es válida (no impide conservar el documento). No implica que la "
            "identidad del firmante esté verificada contra una autoridad certificadora."
        )
    )
    firma_firmante: str | None = Field(description="Firmante declarado por la propia firma, sin valor si `firma_valida` no lo tiene.")
    firma_fecha: datetime | None = Field(description="Fecha de firma declarada por la propia firma, sin valor si `firma_valida` no lo tiene.")
    estado_autenticacion: EstadoAutenticacionDocumento
    estado: EstadoDocumento
    # CU-10: el documento que este reemplazo, si llego por sustitucion explicita. Se
    # expone para que la sustitucion no sea una caja negra: el ciudadano puede seguir el
    # historial hacia atras consultando ese id con GET /documentos/{id}.
    sustituye_a: uuid.UUID | None
    tamano_bytes: int
    hash_sha256: str
    creado_en: datetime


class RespuestaListaDocumentos(BaseModel):
    items: list[RespuestaDocumento]
    total: int
    page: int
    size: int


class RespuestaDescarga(BaseModel):
    url: str
    expira_en: datetime


class RespuestaAutenticacion(BaseModel):
    estado: EstadoAutenticacionDocumento
    # El contrato documentado para el POST solo muestra {"estado": "PENDIENTE"}; estos
    # dos campos son una extension aditiva (igual forma que el GET) para que A1 pueda
    # "mostrar el resultado guardado" sin cambiar de forma segun el caso.
    respuesta_centralizador: str | None
    actualizado_en: datetime | None


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _a_respuesta(d: Documento) -> RespuestaDocumento:
    return RespuestaDocumento(
        id=d.id,
        titulo=d.titulo,
        tipo=d.tipo,
        entidad_emisora=d.entidad_emisora,
        fecha_emision=d.fecha_emision.date() if d.fecha_emision else None,
        certificado=d.certificado,
        firma_valida=d.firma_valida,
        firma_firmante=d.firma_firmante,
        firma_fecha=d.firma_fecha,
        estado_autenticacion=d.estado_autenticacion,
        estado=d.estado,
        sustituye_a=d.sustituye_a_id,
        tamano_bytes=d.tamano_bytes,
        hash_sha256=d.hash_sha256,
        creado_en=d.creado_en,
    )


def _a_respuesta_autenticacion(d: Documento) -> RespuestaAutenticacion:
    return RespuestaAutenticacion(
        estado=d.estado_autenticacion,
        respuesta_centralizador=d.respuesta_centralizador,
        actualizado_en=d.autenticacion_actualizada_en,
    )


@router.post("", response_model=RespuestaDocumento)
async def cargar_documento(
    response: Response,
    request: Request,
    archivo: UploadFile = File(...),
    titulo: str = Form(..., min_length=1, max_length=255),
    tipo: str = Form(..., min_length=1, max_length=100),
    entidad_emisora: str | None = Form(None, max_length=255),
    fecha_emision: date | None = Form(None),
    sustituye_a: uuid.UUID | None = Form(None),
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaDocumento:
    """Carga un documento en la carpeta del ciudadano autenticado.

    Recibe el archivo (`archivo`, PDF/JPEG/PNG) junto con su título, tipo y,
    opcionalmente, la entidad emisora y la fecha de emisión. Si `sustituye_a` incluye
    el id de un documento propio no certificado, ese documento se reemplaza por el
    nuevo. Devuelve 201 con los metadatos del documento creado, o 200 si el archivo ya
    se había cargado antes (mismo contenido) y no se crea uno nuevo.

    Si el archivo es un PDF con una firma digital embebida, su validez criptográfica
    se revisa poco después de la carga y queda en `firma_valida`; en la respuesta
    inmediata todavía puede aparecer sin valor. `firma_valida` sin valor significa que
    el documento no tiene firma que validar (o que la validación todavía no terminó);
    `false` significa que sí se validó y no es una firma válida. Una firma inválida no
    impide guardar el documento. Esa validación no comprueba la identidad del
    firmante contra ninguna autoridad certificadora: solo que el contenido no cambió
    desde que se firmó y que la firma en sí es criptográficamente correcta;
    `firma_firmante` y `firma_fecha` son los datos que la propia firma declara.

    Puede rechazar la carga con 413 si el archivo excede el tamaño máximo, 415 si el
    tipo de archivo no está permitido, 409 si la cuota de almacenamiento está agotada
    o si `sustituye_a` corresponde a un documento certificado, 404 si `sustituye_a` no
    existe, o 403 si no pertenece al ciudadano autenticado.
    """
    contenido = await archivo.read()
    documento, fue_creado = await servicios.cargar_documento(
        ciudadano_id=actual.id,
        contenido=contenido,
        titulo=titulo,
        tipo=tipo,
        entidad_emisora=entidad_emisora,
        fecha_emision=fecha_emision,
        sustituye_a=sustituye_a,
        correlation_id=_correlation_id(request),
    )
    response.status_code = 201 if fue_creado else 200
    return _a_respuesta(documento)


@router.get("", response_model=RespuestaListaDocumentos)
async def listar_documentos(
    tipo: str | None = None,
    entidad: str | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    certificado: bool | None = None,
    estado_autenticacion: EstadoAutenticacionDocumento | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = servicios.TAMANO_PAGINA_DEFECTO,
    actual: Ciudadano = Depends(ciudadano_actual),
) -> RespuestaListaDocumentos:
    """Busca y clasifica los documentos del ciudadano autenticado (CU-06, CU-07).

    Acepta filtros opcionales por tipo (`tipo`), entidad emisora (`entidad`, coincidencia
    parcial), rango de fecha de emisión (`desde`/`hasta`), estado de certificación
    (`certificado`), estado de autenticación ante GovCarpeta (`estado_autenticacion`) y
    texto en el título (`q`), más paginación (`page`, `size`; tamaño de página máximo
    100). Devuelve los documentos más recientes primero, junto con el total de
    resultados que coinciden con los filtros. Solo incluye los documentos vigentes de la
    carpeta: los reemplazados por una versión más reciente o eliminados no aparecen
    aquí, aunque siguen siendo consultables por su id.
    """
    items, total, page, size = await servicios.listar_documentos(
        ciudadano_id=actual.id,
        tipo=tipo,
        entidad=entidad,
        desde=desde,
        hasta=hasta,
        certificado=certificado,
        estado_autenticacion=estado_autenticacion,
        q=q,
        page=page,
        size=size,
    )
    return RespuestaListaDocumentos(items=[_a_respuesta(d) for d in items], total=total, page=page, size=size)


@router.get("/{documento_id}", response_model=RespuestaDocumento)
async def obtener_documento(documento_id: uuid.UUID, actual: Ciudadano = Depends(ciudadano_actual)) -> RespuestaDocumento:
    """Consulta los metadatos de un documento propio, incluido uno ya reemplazado por
    una versión más reciente (para seguir su historial con `sustituye_a`).

    Devuelve 404 si el documento no existe o fue eliminado, o 403 si no pertenece al
    ciudadano autenticado.
    """
    documento = await servicios.obtener_documento(ciudadano_id=actual.id, documento_id=documento_id)
    return _a_respuesta(documento)


@router.delete("/{documento_id}", status_code=204, response_model=None)
async def eliminar_documento(
    documento_id: uuid.UUID, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> None:
    """Elimina un documento no certificado de la carpeta del ciudadano autenticado.

    El borrado es diferido: el documento deja de listarse y de ser accesible de
    inmediato, pero el objeto se conserva en el almacenamiento hasta la purga física.
    Devuelve 404 si el documento no existe o ya fue eliminado, 403 si no pertenece al
    ciudadano autenticado, 409 si el documento está certificado, o 409 si ya fue
    reemplazado por una versión más reciente.
    """
    await servicios.eliminar_documento(ciudadano_id=actual.id, documento_id=documento_id, correlation_id=_correlation_id(request))


@router.get("/{documento_id}/descarga", response_model=RespuestaDescarga)
async def descargar_documento(
    documento_id: uuid.UUID, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaDescarga:
    """Genera un enlace temporal para descargar un documento propio.

    Devuelve una URL firmada y la fecha en que expira; la URL no se reutiliza ni se
    almacena, y deja de funcionar una vez vencida. Devuelve 404 si el documento no
    existe, o 403 si no pertenece al ciudadano autenticado.
    """
    url, expira_en = await servicios.generar_descarga(
        ciudadano_id=actual.id, documento_id=documento_id, correlation_id=_correlation_id(request)
    )
    return RespuestaDescarga(url=url, expira_en=expira_en)


@router.post("/{documento_id}/autenticacion", status_code=202)
async def solicitar_autenticacion(
    documento_id: uuid.UUID, response: Response, request: Request, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaAutenticacion:
    """Solicita la autenticación de un documento propio ante GovCarpeta (MinTIC).

    La operación es asíncrona: responde 202 con `estado: PENDIENTE` y el resultado se
    consulta luego con `GET` sobre esta misma ruta. Si el documento ya estaba
    autenticado, responde 200 con el resultado guardado en vez de solicitarlo de nuevo;
    si ya había una solicitud en curso, responde 202 sin duplicarla. Devuelve 404 si el
    documento no existe, 403 si no pertenece al ciudadano autenticado, o 409 si el
    documento ya no está vigente (fue reemplazado por una versión más reciente).
    """
    documento, status_code = await servicios.solicitar_autenticacion(
        ciudadano_id=actual.id, documento_id=documento_id, correlation_id=_correlation_id(request)
    )
    response.status_code = status_code
    return _a_respuesta_autenticacion(documento)


@router.get("/{documento_id}/autenticacion", response_model=RespuestaAutenticacion)
async def consultar_autenticacion(
    documento_id: uuid.UUID, actual: Ciudadano = Depends(ciudadano_actual)
) -> RespuestaAutenticacion:
    """Consulta el resultado de la autenticación de un documento propio ante GovCarpeta.

    El estado es `NO_SOLICITADA` si nunca se pidió, `PENDIENTE` mientras se procesa,
    y `AUTENTICADO` o `RECHAZADO` con el resultado final. Devuelve 404 si el documento
    no existe, o 403 si no pertenece al ciudadano autenticado.
    """
    documento = await servicios.consultar_autenticacion(ciudadano_id=actual.id, documento_id=documento_id)
    return _a_respuesta_autenticacion(documento)
