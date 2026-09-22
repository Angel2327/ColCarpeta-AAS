"""Alta, rotacion de credencial, revocacion y reactivacion de una entidad emisora
para CU-13.

No es una ruta publica a proposito: se corre manualmente, con acceso directo a la base
de datos -- misma filosofia que scripts/limpiar_prueba.py. CU-04 completo (registro
publico de entidades) no esta implementado; esto es lo minimo que CU-13 necesita para
tener un emisor real contra el que probar.

Subcomandos:

  alta       Crea la entidad, o si ya existe actualiza su nombre y ROTA su clave (la
             anterior deja de servir de inmediato). No toca el estado: una entidad
             revocada sigue revocada tras rotar su clave -- hace falta "reactivar"
             aparte para que vuelva a autenticarse.
  revocar    Marca la entidad como REVOCADA: deja de poder autenticarse de inmediato
             (app.documentos.entidades.entidad_actual la rechaza), pero la fila y su
             historia (auditoria, documentos ya depositados) se conservan intactas.
  reactivar  Vuelve a ACTIVA una entidad revocada, con la MISMA clave que ya tenia --
             revocar no la invalida, solo bloquea su uso. Si se perdio o se quiere
             invalidar esa clave, corre "alta" despues para rotarla.

Uso:
    python scripts/alta_entidad_emisora.py alta --id 900123456 --nombre "Universidad EAFIT"
    python scripts/alta_entidad_emisora.py revocar --id 900123456
    python scripts/alta_entidad_emisora.py reactivar --id 900123456

La clave de API en claro solo se imprime una vez, al darla de alta o rotarla; solo su
hash queda en la base (EntidadEmisora.api_key_hash), igual esquema que el token de
primer acceso. Se usa en el encabezado X-Api-Key al llamar
POST /api/v1/entidades/documentos.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.identidad.token_acceso import generar_token  # noqa: E402
from app.models import EntidadEmisora, EstadoEntidadEmisora  # noqa: E402


async def _alta(entidad_id: str, nombre: str) -> None:
    clave, clave_hash = generar_token()
    async with SessionLocal() as session:
        entidad = await session.get(EntidadEmisora, entidad_id)
        if entidad is None:
            session.add(EntidadEmisora(id=entidad_id, nombre=nombre, api_key_hash=clave_hash))
            print(f"Entidad nueva: {entidad_id} ({nombre})")
        else:
            entidad.nombre = nombre
            entidad.api_key_hash = clave_hash
            print(f"Entidad existente actualizada, clave rotada: {entidad_id} ({nombre}), estado sin cambios ({entidad.estado.value})")
        await session.commit()

    print(f"\nClave de API (unica vez, guardala ahora):\n{clave}\n")
    print("Usala en el encabezado X-Api-Key al llamar POST /api/v1/entidades/documentos.")


async def _cambiar_estado(entidad_id: str, nuevo_estado: EstadoEntidadEmisora) -> None:
    async with SessionLocal() as session:
        entidad = await session.get(EntidadEmisora, entidad_id)
        if entidad is None:
            print(f"No existe ninguna entidad con id {entidad_id}.")
            sys.exit(1)
        if entidad.estado == nuevo_estado:
            print(f"Entidad {entidad_id} ({entidad.nombre}) ya estaba en estado {nuevo_estado.value}; sin cambios.")
            return
        entidad.estado = nuevo_estado
        await session.commit()
        print(f"Entidad {entidad_id} ({entidad.nombre}) -> {nuevo_estado.value}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="accion", required=True)

    p_alta = sub.add_parser("alta", help="Crea la entidad, o rota su clave si ya existe")
    p_alta.add_argument("--id", required=True, help="Identificacion de la entidad (p. ej. NIT)")
    p_alta.add_argument("--nombre", required=True)

    p_revocar = sub.add_parser("revocar", help="Revoca la entidad: deja de autenticarse, conserva su historia")
    p_revocar.add_argument("--id", required=True)

    p_reactivar = sub.add_parser("reactivar", help="Reactiva una entidad revocada, con la clave que ya tenia")
    p_reactivar.add_argument("--id", required=True)

    args = parser.parse_args()

    if args.accion == "alta":
        await _alta(args.id, args.nombre)
    elif args.accion == "revocar":
        await _cambiar_estado(args.id, EstadoEntidadEmisora.REVOCADA)
    elif args.accion == "reactivar":
        await _cambiar_estado(args.id, EstadoEntidadEmisora.ACTIVA)


if __name__ == "__main__":
    asyncio.run(main())
