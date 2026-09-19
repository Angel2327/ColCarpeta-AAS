"""Cliente del centralizador del MinTIC (GovCarpeta).

UNICO modulo del proyecto que habla con el centralizador. Ningun otro modulo debe
importarlo directamente: se accede a traves de los servicios de este paquete.

Contratos verificados contra el servicio en produccion el 19/09/2026. Las trampas del
contrato estan documentadas en CLAUDE.md y en docs/especificacion.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import get_config

TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class CentralizadorNoDisponible(Exception):
    """El centralizador devolvio 5xx, agoto el tiempo de espera o no fue alcanzable."""


@dataclass(frozen=True)
class Operador:
    id: str
    nombre: str
    transfer_api_url: str | None

    @property
    def puede_recibir_transferencias(self) -> bool:
        return bool(self.transfer_api_url)


@dataclass(frozen=True)
class ResultadoValidacion:
    """Resultado de validateCitizen.

    OJO: la semantica del centralizador esta invertida respecto a lo intuitivo.
      204 -> el ciudadano NO esta afiliado a ningun operador  -> disponible
      200 -> el ciudadano YA esta afiliado a otro operador    -> no disponible
    """

    disponible: bool
    mensaje: str | None = None


class GovCarpeta:
    def __init__(self, cliente: httpx.AsyncClient | None = None) -> None:
        cfg = get_config()
        self._base = cfg.govcarpeta_url.rstrip("/")
        self._operator_id = cfg.operator_id
        self._operator_name = cfg.operator_name
        self._cliente = cliente

    async def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(base_url=self._base, timeout=TIMEOUT)
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None

    @staticmethod
    def _falla(respuesta: httpx.Response) -> None:
        if respuesta.status_code >= 500:
            raise CentralizadorNoDisponible(
                f"{respuesta.status_code} en {respuesta.request.url}: {respuesta.text[:200]}"
            )

    # ------------------------------------------------------------------ ciudadanos

    async def validar_ciudadano(self, cedula: int) -> ResultadoValidacion:
        """GET /apis/validateCitizen/{id}

        La decision se toma SIEMPRE por el codigo HTTP, nunca por el cuerpo.
        """
        http = await self._http()
        try:
            r = await http.get(f"/apis/validateCitizen/{cedula}")
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        self._falla(r)
        if r.status_code == 204:
            return ResultadoValidacion(disponible=True)
        if r.status_code == 200:
            # El texto nombra al operador actual; es informativo, no contractual.
            return ResultadoValidacion(disponible=False, mensaje=r.text.strip().strip('"'))
        raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")

    async def registrar_ciudadano(
        self, *, cedula: int, nombre: str, direccion: str, email: str
    ) -> None:
        """POST /apis/registerCitizen. 201 creado, 501 ya registrado."""
        http = await self._http()
        cuerpo = {
            "id": cedula,
            "name": nombre,
            "address": direccion,
            "email": email,
            "operatorId": self._operator_id,
            "operatorName": self._operator_name,
        }
        try:
            r = await http.post("/apis/registerCitizen", json=cuerpo)
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        if r.status_code == 201:
            return
        if r.status_code == 501:
            # No es transitorio: no tiene sentido reintentar.
            raise ValueError(f"el centralizador rechazo el registro: {r.text[:200]}")
        self._falla(r)
        raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")

    async def desligar_ciudadano(self, cedula: int) -> None:
        """DELETE /apis/unregisterCitizen.

        OJO: lleva cuerpo JSON. httpx no admite cuerpo en el atajo .delete().
        """
        http = await self._http()
        cuerpo = {
            "id": cedula,
            "operatorId": self._operator_id,
            "operatorName": self._operator_name,
        }
        try:
            r = await http.request("DELETE", "/apis/unregisterCitizen", json=cuerpo)
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        if r.status_code in (200, 201, 204):
            return
        self._falla(r)
        raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")

    # ------------------------------------------------------------------ documentos

    async def autenticar_documento(
        self, *, cedula: int, url_documento: str, titulo: str
    ) -> str:
        """PUT /apis/authenticateDocument.

        OJO: el campo es `UrlDocument`, con U mayuscula inicial. El centralizador
        descarga esa URL, asi que debe ser un enlace firmado alcanzable sin credenciales.
        """
        http = await self._http()
        cuerpo = {
            "idCitizen": cedula,
            "UrlDocument": url_documento,
            "documentTitle": titulo,
        }
        try:
            r = await http.put("/apis/authenticateDocument", json=cuerpo)
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        if r.status_code == 200:
            return r.text.strip()
        if r.status_code == 204:
            raise ValueError("el centralizador no autentico el documento")
        self._falla(r)
        raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")

    # ------------------------------------------------------------------ operadores

    async def listar_operadores(self) -> list[Operador]:
        """GET /apis/getOperators.

        El directorio es dato sucio: nombres duplicados, URLs con espacios al inicio y
        entradas sin transferAPIURL. Se normaliza aqui y se resuelve por _id.
        """
        http = await self._http()
        try:
            r = await http.get("/apis/getOperators")
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        self._falla(r)
        if r.status_code != 200:
            raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")
        operadores = []
        for fila in r.json():
            url = (fila.get("transferAPIURL") or "").strip()
            operadores.append(
                Operador(
                    id=fila["_id"],
                    nombre=(fila.get("operatorName") or "").strip(),
                    transfer_api_url=url or None,
                )
            )
        return operadores

    async def publicar_endpoints_de_transferencia(
        self, *, endpoint: str, endpoint_confirmacion: str
    ) -> None:
        """PUT /apis/registerTransferEndPoint.

        Ejecutar SOLO cuando las dos rutas respondan correctamente: el directorio es
        publico y otros operadores empezaran a enviar ciudadanos apenas se publique.
        """
        http = await self._http()
        cuerpo = {
            "idOperator": self._operator_id,
            "endPoint": endpoint,
            "endPointConfirm": endpoint_confirmacion,
        }
        try:
            r = await http.put("/apis/registerTransferEndPoint", json=cuerpo)
        except httpx.HTTPError as exc:
            raise CentralizadorNoDisponible(str(exc)) from exc
        if r.status_code in (200, 201):
            return
        self._falla(r)
        raise CentralizadorNoDisponible(f"respuesta inesperada {r.status_code}")
