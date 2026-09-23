"""Prueba aislada de `_reconciliar_transferencias` ("Aceptacion de confirmaciones"):
una transferencia que quedo `ENVIADA` sin confirmar durante mas de
`TRANSFER_CONFIRM_TIMEOUT` se resuelve consultando `validateCitizen`, en vez de quedar
colgada para siempre. No espera horas de verdad: inserta directamente una fila
`transferencia` con `enviada_en` retrocedido en la base, y corre
`_reconciliar_transferencias` una sola vez.

Corre dentro de docker-compose.test.yml (servicio "prueba-reconciliacion"), con
DATABASE_URL apuntando a postgres-a y GOVCARPETA_URL a mock-centralizador -- nunca
contra el MinTIC real ni la base de Supabase.

La pregunta que responde la reconciliacion es una sola: ¿el ciudadano sigue siendo
nuestro? Cuatro escenarios en la misma corrida, cada uno con su propia cedula y su
propia fila `transferencia`:

  A. El centralizador dice que el ciudadano SI quedo afiliado al destino (200,
     nombrando exactamente el operador de la transferencia). Ya no es nuestro. Se
     espera: transferencia -> CONFIRMADA, purgar_despues_de programado, y el
     ciudadano local (si esta EN_TRANSFERENCIA) -> TRASLADADO. Mismo desenlace que un
     req_status=1 real.
  B. El centralizador dice que el ciudadano esta disponible (204, nadie lo afilio). No
     es de nadie. Se espera: transferencia -> FALLIDA, ciudadano ->
     PENDIENTE_CENTRALIZADOR, y un registerCitizen encolado. Mismo desenlace que un
     req_status=0 real.
  C. El centralizador dice 200 pero nombra a un operador DISTINTO del destino de esta
     transferencia. Sigue sin ser nuestro, aunque no haya llegado exactamente a donde
     lo mandamos: la pregunta es si sigue siendo nuestro, no si llego a destino. Se
     espera el mismo desenlace que A: CONFIRMADA.
  D. El centralizador dice 200 y nombra a ColCarpeta MISMO (el `unregisterCitizen` del
     envio original no surtio efecto: el ciudadano nunca se desafilio de nosotros). Se
     espera el mismo desenlace que B: FALLIDA y recuperado -- sigue siendo tan nuestro
     como si nunca se hubiera afiliado a nadie.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.interoperabilidad.govcarpeta import GovCarpeta  # noqa: E402
from app.interoperabilidad.outbox import _reconciliar_transferencias  # noqa: E402
from app.models import (  # noqa: E402
    Ciudadano,
    EstadoCiudadano,
    EstadoTransferencia,
    OperadorCache,
    Outbox,
    Transferencia,
)

CEDULA_A = 900200301
CEDULA_B = 900200302
CEDULA_C = 900200303
CEDULA_D = 900200304


async def _limpiar(cedula: int, operador_id: str) -> None:
    async with SessionLocal() as session:
        for fila in (await session.execute(select(Outbox).where(Outbox.payload["cedula"].astext == str(cedula)))).scalars():
            await session.delete(fila)
        for fila in (await session.execute(select(Transferencia).where(Transferencia.ciudadano_id == cedula))).scalars():
            await session.delete(fila)
        ciudadano = await session.get(Ciudadano, cedula)
        if ciudadano is not None:
            await session.delete(ciudadano)
        operador = await session.get(OperadorCache, operador_id)
        if operador is not None:
            await session.delete(operador)
        await session.commit()


async def _preparar(
    *, cedula: int, nombre: str, operador_id: str, operador_nombre: str, timeout_segundos: int
) -> int:
    """Crea el ciudadano EN_TRANSFERENCIA, el operador_cache del destino, y la fila
    `transferencia` ENVIADA con `enviada_en` ya vencido (retrocedido en la base, no
    una espera real). Devuelve el id de la transferencia."""
    async with SessionLocal() as session:
        session.add(OperadorCache(id=operador_id, nombre=operador_nombre, transfer_api_url="https://ejemplo.invalido/api/transferCitizen"))
        session.add(
            Ciudadano(
                id=cedula,
                nombre=nombre,
                direccion="",
                email_carpeta=f"{cedula}@carpetacolombia.co",
                email_personal="",
                telefono="",
                password_hash=None,
                estado=EstadoCiudadano.EN_TRANSFERENCIA,
                identidad_verificada=True,
            )
        )
        transferencia = Transferencia(
            ciudadano_id=cedula,
            operador_destino_id=operador_id,
            confirm_api="https://ejemplo.invalido/api/transferCitizenConfirm",
            estado=EstadoTransferencia.ENVIADA,
        )
        session.add(transferencia)
        await session.flush()
        # "Retrocede enviada_en en la base": no se espera de verdad TRANSFER_CONFIRM_TIMEOUT.
        transferencia.enviada_en = datetime.now(timezone.utc) - timedelta(seconds=timeout_segundos + 60)
        transferencia_id = transferencia.id
        await session.commit()
    return transferencia_id


async def main() -> None:
    cfg = get_config()
    fallos: list[str] = []

    escenarios = (
        (CEDULA_A, "test-recon-a"),
        (CEDULA_B, "test-recon-b"),
        (CEDULA_C, "test-recon-c"),
        (CEDULA_D, "test-recon-d"),
    )
    for cedula, operador_id in escenarios:
        await _limpiar(cedula, operador_id)

    print("Preparando los cuatro escenarios (transferencia ENVIADA con enviada_en vencido)...")
    id_a = await _preparar(
        cedula=CEDULA_A, nombre="Reconciliacion Confirmada", operador_id="test-recon-a",
        operador_nombre="Operador Reconciliacion A", timeout_segundos=cfg.transfer_confirm_timeout,
    )
    id_b = await _preparar(
        cedula=CEDULA_B, nombre="Reconciliacion Recuperada", operador_id="test-recon-b",
        operador_nombre="Operador Reconciliacion B", timeout_segundos=cfg.transfer_confirm_timeout,
    )
    id_c = await _preparar(
        cedula=CEDULA_C, nombre="Reconciliacion Confirmada Con Tercero", operador_id="test-recon-c",
        operador_nombre="Operador Reconciliacion C", timeout_segundos=cfg.transfer_confirm_timeout,
    )
    id_d = await _preparar(
        cedula=CEDULA_D, nombre="Reconciliacion Sigue Nuestro", operador_id="test-recon-d",
        operador_nombre="Operador Reconciliacion D", timeout_segundos=cfg.transfer_confirm_timeout,
    )
    print(f"   -> transferencias: A={id_a} B={id_b} C={id_c} D={id_d}")

    print(
        "Preparando el centralizador falso: A queda afiliado al destino correcto, "
        "C queda afiliado a un operador DISTINTO del destino, D queda afiliado a "
        f"ColCarpeta mismo ({cfg.operator_name!r}, como si unregisterCitizen no hubiera "
        "surtido efecto), B no se registra (disponible)."
    )
    async with httpx.AsyncClient(base_url=cfg.govcarpeta_url, timeout=10.0) as cliente:
        r = await cliente.post("/apis/registerCitizen", json={
            "id": CEDULA_A, "operatorId": "test-recon-a", "operatorName": "Operador Reconciliacion A",
            "name": "Reconciliacion Confirmada", "address": "", "email": f"{CEDULA_A}@carpetacolombia.co",
        })
        print(f"   -> registerCitizen A: {r.status_code}")
        r = await cliente.post("/apis/registerCitizen", json={
            "id": CEDULA_C, "operatorId": "otro-operador-no-relacionado", "operatorName": "Un Tercero Cualquiera",
            "name": "Reconciliacion Confirmada Con Tercero", "address": "", "email": f"{CEDULA_C}@carpetacolombia.co",
        })
        print(f"   -> registerCitizen C (a un tercero, no al destino de su transferencia): {r.status_code}")
        r = await cliente.post("/apis/registerCitizen", json={
            "id": CEDULA_D, "operatorId": cfg.operator_id, "operatorName": cfg.operator_name,
            "name": "Reconciliacion Sigue Nuestro", "address": "", "email": f"{CEDULA_D}@carpetacolombia.co",
        })
        print(f"   -> registerCitizen D (a ColCarpeta mismo, como si unregisterCitizen no hubiera corrido): {r.status_code}")

    print("\nCorriendo _reconciliar_transferencias() una sola vez...")
    gov = GovCarpeta()
    try:
        resueltas = await _reconciliar_transferencias(gov, SessionLocal)
    finally:
        await gov.cerrar()
    print(f"   -> resolvio {resueltas} fila(s)")

    print("\nEscenario A (afiliado al destino exacto, deberia quedar CONFIRMADA):")
    async with SessionLocal() as session:
        t = await session.get(Transferencia, id_a)
        c = await session.get(Ciudadano, CEDULA_A)
        print(f"   transferencia.estado={t.estado} purgar_despues_de={t.purgar_despues_de}")
        print(f"   ciudadano.estado={c.estado}")
        if t.estado != EstadoTransferencia.CONFIRMADA:
            fallos.append(f"A: se esperaba CONFIRMADA, quedo {t.estado}")
        if t.purgar_despues_de is None:
            fallos.append("A: se esperaba purgar_despues_de programado")
        if c.estado != EstadoCiudadano.TRASLADADO:
            fallos.append(f"A: se esperaba ciudadano TRASLADADO, quedo {c.estado}")

    print("\nEscenario B (disponible, deberia quedar FALLIDA y recuperado):")
    async with SessionLocal() as session:
        t = await session.get(Transferencia, id_b)
        c = await session.get(Ciudadano, CEDULA_B)
        print(f"   transferencia.estado={t.estado}")
        print(f"   ciudadano.estado={c.estado}")
        if t.estado != EstadoTransferencia.FALLIDA:
            fallos.append(f"B: se esperaba FALLIDA, quedo {t.estado}")
        if c.estado != EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
            fallos.append(f"B: se esperaba ciudadano PENDIENTE_CENTRALIZADOR, quedo {c.estado}")
        reencolado = (await session.execute(
            select(Outbox).where(Outbox.operacion == "registerCitizen", Outbox.payload["cedula"].astext == str(CEDULA_B))
        )).scalars().first()
        print(f"   registerCitizen reencolado: {reencolado is not None}")
        if reencolado is None:
            fallos.append("B: se esperaba un registerCitizen reencolado para recuperar al ciudadano")

    print("\nEscenario C (afiliado a un tercero distinto del destino, deberia quedar CONFIRMADA igual que A):")
    async with SessionLocal() as session:
        t = await session.get(Transferencia, id_c)
        c = await session.get(Ciudadano, CEDULA_C)
        print(f"   transferencia.estado={t.estado} purgar_despues_de={t.purgar_despues_de}")
        print(f"   ciudadano.estado={c.estado}")
        if t.estado != EstadoTransferencia.CONFIRMADA:
            fallos.append(f"C: se esperaba CONFIRMADA (ya no es nuestro, sin importar en que operador quedo), quedo {t.estado}")
        if c.estado != EstadoCiudadano.TRASLADADO:
            fallos.append(f"C: se esperaba ciudadano TRASLADADO, quedo {c.estado}")

    print("\nEscenario D (sigue afiliado a ColCarpeta mismo, deberia quedar FALLIDA y recuperado igual que B):")
    async with SessionLocal() as session:
        t = await session.get(Transferencia, id_d)
        c = await session.get(Ciudadano, CEDULA_D)
        print(f"   transferencia.estado={t.estado}")
        print(f"   ciudadano.estado={c.estado}")
        if t.estado != EstadoTransferencia.FALLIDA:
            fallos.append(f"D: se esperaba FALLIDA (unregisterCitizen no surtio efecto, sigue siendo nuestro), quedo {t.estado}")
        if c.estado != EstadoCiudadano.PENDIENTE_CENTRALIZADOR:
            fallos.append(f"D: se esperaba ciudadano PENDIENTE_CENTRALIZADOR, quedo {c.estado}")
        reencolado = (await session.execute(
            select(Outbox).where(Outbox.operacion == "registerCitizen", Outbox.payload["cedula"].astext == str(CEDULA_D))
        )).scalars().first()
        print(f"   registerCitizen reencolado: {reencolado is not None}")
        if reencolado is None:
            fallos.append("D: se esperaba un registerCitizen reencolado para recuperar al ciudadano")

    print("\nLimpiando datos de prueba...")
    for cedula, operador_id in escenarios:
        await _limpiar(cedula, operador_id)

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f" - {f}")
        sys.exit(1)
    print("Reconciliacion probada de punta a punta (A, B, C y D) sin fallos.")


if __name__ == "__main__":
    asyncio.run(main())
