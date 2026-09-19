"""Prueba de humo del cliente del centralizador contra la API real.

    python scripts/probar_govcarpeta.py

No registra ni desliga a nadie: solo ejecuta las operaciones de lectura.
"""

import asyncio

from app.interoperabilidad.govcarpeta import GovCarpeta


async def main() -> None:
    gov = GovCarpeta()
    try:
        libre = await gov.validar_ciudadano(79999999)
        print(f"cedula libre      -> disponible={libre.disponible}  (se espera True)")

        ocupada = await gov.validar_ciudadano(1234567890)
        print(f"cedula ocupada    -> disponible={ocupada.disponible}  (se espera False)")
        print(f"  mensaje: {ocupada.mensaje}")

        operadores = await gov.listar_operadores()
        con_endpoint = [o for o in operadores if o.puede_recibir_transferencias]
        print(f"directorio        -> {len(operadores)} operadores, {len(con_endpoint)} con endpoint")

        propio = [o for o in operadores if o.id == gov._operator_id]
        print(f"ColCarpeta en el directorio -> {'si' if propio else 'NO'}")
    finally:
        await gov.cerrar()


if __name__ == "__main__":
    asyncio.run(main())
