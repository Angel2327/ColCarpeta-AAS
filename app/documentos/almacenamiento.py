"""Cliente del bucket de objetos (docs/especificacion.md, "Seguridad y manejo de
documentos" > Almacenamiento). Unico modulo que habla con el almacenamiento.

El bucket es privado y las claves de objeto se generan como identificadores aleatorios,
sin cedula ni nombre del ciudadano.
"""

from __future__ import annotations

import uuid

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_config

_EXTENSION_POR_TIPO = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
}


class FalloAlmacenamiento(Exception):
    """El bucket no acepto la operacion (subida, borrado o firma de enlace)."""


def _cliente():
    cfg = get_config()
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_endpoint or None,
        aws_access_key_id=cfg.s3_access_key or None,
        aws_secret_access_key=cfg.s3_secret_key or None,
        region_name=cfg.s3_region,
        config=BotoConfig(signature_version="s3v4"),
    )


def generar_clave(content_type: str) -> str:
    extension = _EXTENSION_POR_TIPO.get(content_type, "bin")
    return f"documentos/{uuid.uuid4().hex}.{extension}"


def subir_objeto(*, clave: str, contenido: bytes, content_type: str) -> None:
    cfg = get_config()
    try:
        _cliente().put_object(Bucket=cfg.s3_bucket, Key=clave, Body=contenido, ContentType=content_type)
    except (BotoCoreError, ClientError) as exc:
        raise FalloAlmacenamiento(str(exc)) from exc


def eliminar_objeto(*, clave: str) -> None:
    cfg = get_config()
    try:
        _cliente().delete_object(Bucket=cfg.s3_bucket, Key=clave)
    except (BotoCoreError, ClientError) as exc:
        raise FalloAlmacenamiento(str(exc)) from exc


def generar_url_descarga(*, clave: str, ttl_segundos: int) -> str:
    cfg = get_config()
    try:
        return _cliente().generate_presigned_url(
            "get_object", Params={"Bucket": cfg.s3_bucket, "Key": clave}, ExpiresIn=ttl_segundos
        )
    except (BotoCoreError, ClientError) as exc:
        raise FalloAlmacenamiento(str(exc)) from exc


def descargar_objeto(*, clave: str) -> bytes:
    """Trae el objeto de vuelta al proceso (CU-09: la bandeja de salida necesita los
    bytes reales para validar la firma, no un enlace -- a diferencia de CU-11, que solo
    le da al centralizador un enlace firmado para que lo descargue el mismo)."""
    cfg = get_config()
    try:
        return _cliente().get_object(Bucket=cfg.s3_bucket, Key=clave)["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise FalloAlmacenamiento(str(exc)) from exc
