"""backfill origen registro directo para ciudadanos previos a la migracion

Revision ID: 567f21d2ce7a
Revises: b1fb337fb116
Create Date: 2026-09-24 18:24:39.801972

Migracion de DATOS (no de esquema): la migracion anterior (b1fb337fb116) agrego la
columna `ciudadano.origen`, nullable y sin backfill a proposito -- los ciudadanos que
ya existian quedaron con `origen IS NULL`, y la consola de administracion los muestra
como "Desconocido". Esta migracion resuelve ese "Desconocido" para los casos donde el
origen SI se puede afirmar como un hecho comprobado, no una suposicion. El valor
"Desconocido" se queda en el codigo de todas formas (app.admin.servicios._origen_mostrar):
sigue siendo correcto para cualquier fila futura que por alguna razon no traiga origen.
Lo que se quita aqui es que aparezca en datos donde si se sabe la respuesta.

--- Lo que se comprobo contra la base real, antes de escribir una sola linea de esta
    migracion (consultas de solo lectura, ejecutadas el 2026-09-24) ---

1. Cuantos ciudadanos con origen sin valor, y cuales son:

   SELECT id, nombre, estado, creado_en FROM ciudadano WHERE origen IS NULL;

   Cuatro filas, ninguna mas ni menos:
     - 1122334455  "Prueba Integracion"        ACTIVO   creado 2026-09-19 23:33:02 UTC
     - 1077700123  "Prueba Login"               ACTIVO   creado 2026-09-20 00:42:32 UTC
     - 1032123123  "Angel Martinez"             ACTIVO   creado 2026-09-23 04:06:26 UTC
     - 1034556781  "Mariana Restrepo Ossa"      ACTIVO   creado 2026-09-24 16:48:29 UTC

   Las cuatro fueron creadas ANTES de que b1fb337fb116 agregara la columna `origen`
   (aplicada el 2026-09-24, mas tarde que la mas reciente de estas cuatro) -- son
   exactamente la poblacion que esa migracion dejo deliberadamente sin backfill.

2. Cuantas filas de `outbox` registran una recepcion de transferencia (CU-16), en
   cualquier estado -- la unica forma en que un ciudadano pudo haber llegado por
   TRANSFERENCIA en vez de REGISTRO_DIRECTO:

   SELECT count(*) FROM outbox WHERE operacion = 'receiveTransferCitizen';

   Una sola fila (`outbox.id = 42`), en estado `FALLIDO`, con 3 intentos, creada el
   2026-09-22 03:36:17 UTC. `outbox` no tiene purga automatica (a diferencia de
   `transferencia` y `documento`): esta fila es la historia COMPLETA de recepciones
   intentadas contra esta base desde que el proyecto existe, no una muestra.

3. Esa unica fila no corresponde a ninguno de los cuatro ciudadanos de arriba, ni a
   ningun otro ciudadano que exista hoy:

   - Su `payload->>'cedula'` es `1234567890` -- no aparece en la lista de cuatro, y
     `SELECT 1 FROM ciudadano WHERE id = 1234567890` no devuelve ninguna fila: ese
     ciudadano no existe en la base (si alguna vez se llego a crear en un intento
     anterior antes de fallar, ya no esta).
   - `SELECT * FROM auditoria WHERE accion LIKE 'transferencia.%' ORDER BY momento`
     muestra una docena de eventos entre el 2026-09-22 01:40 y 03:53, todos con
     `recurso` igual a `1234567890` (mas un par de intentos con las cedulas de prueba
     `900000777`/`900000800`/`999999999`) -- nunca con ninguna de las cuatro cedulas
     de arriba.
   - `ultimo_error` de esa fila: "validateCitizen dice que 1234567890 ya esta
     afiliado: El ciudadano con id: 1234567890 se encuentra registrado en el operador:
     Operador Ciudadano" -- la recepcion nunca llego a completarse.

4. Que esa fila es, ademas, imposible de haber originado fuera de esta maquina:

   `payload->>'confirm_api'` es `http://prueba-transferencia:9302/api/transferCitizenConfirm`.
   `prueba-transferencia` es un nombre de host que solo existe dentro de la red
   aislada de Docker que crea `docker-compose.test.yml` (ver ese archivo, servicio
   `prueba-transferencia`, y CLAUDE.md, "Probar en Linux") -- no resuelve en ninguna
   red publica. Ademas, `TRANSFERENCIA_EXIGIR_HTTPS` (por defecto `true`) rechaza
   cualquier intento de ENVIAR una transferencia hacia una URL que no sea `https://`
   (`app.interoperabilidad.traslado_servicios.url_transferencia_utilizable`) -- aunque
   ese chequeo es sobre el envio, no sobre la recepcion, confirma que ningun operador
   de este mismo ecosistema pudo haber apuntado su propio `confirmAPI` real hacia un
   host `http://` interno de Docker: ese valor solo pudo generarlo el propio banco de
   pruebas de este proyecto (`scripts/probar_transferencia.py`), corrido contra esta
   base real durante la sesion "Probar en Linux" documentada en CLAUDE.md
   (2026-09-21/22), usando la cedula 1234567890 -- la que CLAUDE.md documenta como
   "segura" para probar precisamente porque ya esta afiliada a otro operador ante el
   MinTIC real, y por lo tanto `validateCitizen` la rechaza antes de registrar nada de
   verdad.

5. Que ademas, estructuralmente, ningun operador ajeno pudo habernos transferido un
   ciudadano nunca: CLAUDE.md, seccion "Prohibido" -- "No invocar
   `registerTransferEndPoint` contra el MinTIC hasta que /api/transferCitizen y
   /api/transferCitizenConfirm funcionen de verdad" -- el endpoint de transferencia de
   este operador nunca se publico en el directorio publico del MinTIC. Ningun otro
   operador de los 72 que consultan ese directorio supo jamas que ColCarpeta existia
   como destino posible. La unica forma de que `POST /api/transferCitizen` se
   invocara contra esta aplicacion era conociendo la URL de memoria -- exactamente lo
   que hace un script de pruebas propio, nunca un tercero.

--- Conclusion ---

Los cuatro ciudadanos con `origen IS NULL` llegaron por CU-01 (registro directo): no
hay ninguna recepcion de transferencia, real o de prueba, que los mencione, y la unica
recepcion que existe en toda la historia de esta base es prueba interna identificable
por su propio `confirm_api`. Se fija `origen = REGISTRO_DIRECTO` en esos cuatro, y
solo en esos cuatro (por `id`, no por un `WHERE origen IS NULL` generico): si en el
futuro esta migracion se llegara a correr contra una base distinta donde el mismo
razonamiento no aplique, no toca ninguna fila que no haya sido verificada
explicitamente aqui.
"""
from alembic import op
import sqlalchemy as sa


revision = '567f21d2ce7a'
down_revision = 'b1fb337fb116'
branch_labels = None
depends_on = None

# Los cuatro `id` verificados arriba -- nunca un WHERE origen IS NULL generico.
CEDULAS_REGISTRO_DIRECTO = (1122334455, 1077700123, 1032123123, 1034556781)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE ciudadano SET origen = 'REGISTRO_DIRECTO' "
            "WHERE id IN :cedulas AND origen IS NULL"
        ).bindparams(sa.bindparam("cedulas", expanding=True)),
        {"cedulas": list(CEDULAS_REGISTRO_DIRECTO)},
    )


def downgrade() -> None:
    # Reversible solo para estas mismas filas, y solo si siguen en el valor que esta
    # migracion les puso (si alguien las cambio despues por otra razon, no se pisa).
    op.get_bind().execute(
        sa.text(
            "UPDATE ciudadano SET origen = NULL "
            "WHERE id IN :cedulas AND origen = 'REGISTRO_DIRECTO'"
        ).bindparams(sa.bindparam("cedulas", expanding=True)),
        {"cedulas": list(CEDULAS_REGISTRO_DIRECTO)},
    )
