"""CU-03 (traslado de la carpeta a otro operador) -- pantallas del portal. Ver AD-11
(docs/especificacion.md): la lógica de negocio vive en
`app.interoperabilidad.traslado_servicios`, compartida con la ruta JSON
(`app.interoperabilidad.transferencias.router_propio`).

Es la operación más grave que puede tomar un ciudadano en todo el sistema: entrega su
carpeta completa a otro operador y no se puede deshacer. El formulario exige, además
de elegir el operador destino de una lista (nunca texto libre), una casilla de
confirmación explícita (`confirmar`) verificada también del lado del servidor -- el
atributo `required` del navegador es una ayuda, no la protección real; sin la casilla
marcada, la solicitud se rechaza igual que si el operador no existiera.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_config
from app.errors import ErrorDeNegocio
from app.interoperabilidad import traslado_servicios
from app.models import EstadoCiudadano, EstadoTransferencia
from app.portal.auth import ciudadano_actual_portal
from app.portal.router import _correlation_id, templates

router = APIRouter(tags=["portal"], include_in_schema=False)


async def _contexto_estado(ciudadano_id: int) -> dict:
    estado, transferencia, nombre_operador = await traslado_servicios.estado_traslado(ciudadano_id=ciudadano_id)
    # EN_TRANSFERENCIA se marca en la misma transaccion que encola `enviarTransferencia`
    # (app.interoperabilidad.traslado_servicios.solicitar_traslado); la fila
    # `Transferencia` con estado ENVIADA la crea recien el manejador de outbox, un rato
    # despues. Sin contar tambien el estado del ciudadano, la pantalla mostraria "tu
    # carpeta no esta activa" en esa ventana en vez de "tu traslado esta en proceso".
    en_curso = estado == EstadoCiudadano.EN_TRANSFERENCIA or (
        transferencia is not None and transferencia.estado == EstadoTransferencia.ENVIADA
    )
    # Horizonte maximo que el ciudadano puede esperar: si el operador destino nunca
    # confirma, la reconciliacion periodica (app.interoperabilidad.outbox) resuelve la
    # transferencia sola pasado TRANSFER_CONFIRM_TIMEOUT -- vale la pena decirle al
    # ciudadano ese numero en vez de dejarlo mirando un "en proceso" sin horizonte.
    horas_maximo = round(get_config().transfer_confirm_timeout / 3600)
    return {
        "estado_ciudadano": estado,
        "transferencia": transferencia,
        "nombre_operador": nombre_operador,
        "en_curso": en_curso,
        "horas_maximo": horas_maximo,
    }


@router.get("/perfil/traslado", response_class=HTMLResponse)
async def traslado(request: Request) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    contexto = await _contexto_estado(ciudadano.id)
    contexto["ciudadano"] = ciudadano

    if not contexto["en_curso"] and contexto["estado_ciudadano"] == EstadoCiudadano.ACTIVO:
        contexto["operadores"] = await traslado_servicios.listar_operadores_transferibles()

    if request.query_params.get("error"):
        contexto["error"] = request.query_params["error"]

    return templates.TemplateResponse(request, "traslado.html", contexto)


@router.get("/perfil/traslado/estado", response_class=HTMLResponse)
async def traslado_estado(request: Request) -> HTMLResponse:
    """Fragmento sondeado por HTMX mientras el traslado esta `ENVIADA` (esperando
    confirmacion del operador destino) -- deja de traer el atributo de sondeo en
    cuanto el desenlace queda resuelto, para que HTMX pare de pedirlo solo."""
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return HTMLResponse('<div id="traslado-estado"></div>')

    contexto = await _contexto_estado(ciudadano.id)
    return templates.TemplateResponse(request, "_traslado_estado.html", contexto)


@router.post("/perfil/traslado", response_class=HTMLResponse)
async def procesar_traslado(
    request: Request,
    operador_destino_id: str = Form(...),
    confirmar: str | None = Form(None),
) -> HTMLResponse:
    ciudadano = await ciudadano_actual_portal(request)
    if ciudadano is None:
        return RedirectResponse("/sesion", status_code=303)

    if confirmar != "on":
        contexto = await _contexto_estado(ciudadano.id)
        contexto["ciudadano"] = ciudadano
        contexto["operadores"] = await traslado_servicios.listar_operadores_transferibles()
        contexto["error"] = "Debes marcar la casilla de confirmación para trasladar tu carpeta."
        return templates.TemplateResponse(request, "traslado.html", contexto, status_code=200)

    try:
        await traslado_servicios.solicitar_traslado(
            ciudadano_id=ciudadano.id, operador_destino_id=operador_destino_id, correlation_id=_correlation_id(request)
        )
    except ErrorDeNegocio as exc:
        contexto = await _contexto_estado(ciudadano.id)
        contexto["ciudadano"] = ciudadano
        contexto["operadores"] = await traslado_servicios.listar_operadores_transferibles()
        contexto["error"] = exc.mensaje
        return templates.TemplateResponse(request, "traslado.html", contexto, status_code=200)

    return RedirectResponse("/perfil/traslado", status_code=303)
