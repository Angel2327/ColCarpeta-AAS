# ColCarpeta — Operador de Carpeta Ciudadana

Proyecto del curso Arquitecturas Avanzadas de Software. Implementa la solución de **un
operador** de Carpeta Ciudadana. El MinTIC es un actor externo: opera el centralizador,
publica los servicios de interoperabilidad y además opera el operador GovCarpeta.

**La especificación completa está en `docs/especificacion.md`.** Antes de implementar
cualquier cosa, lee la sección correspondiente ahí. Este archivo solo resume lo que hay
que tener presente todo el tiempo.

## Estado actual

La infraestructura está montada y funcionando. **No supongas que falta algo de esto:**

| Elemento                   | Estado                                                                             |
| -------------------------- | ---------------------------------------------------------------------------------- |
| Aplicación desplegada      | `https://colcarpeta-aas-production.up.railway.app` — `/health` y `/docs` responden |
| Base de datos              | PostgreSQL en Supabase, **migrada**. `.env` local tiene las credenciales reales    |
| Bucket `documentos`        | Creado y privado; las credenciales S3 funcionan                                    |
| Operador ante el MinTIC    | Registrado. Aparece en `getOperators`                                              |
| Endpoints de transferencia | **No publicados ante el MinTIC**, a propósito (ver Prohibido)                      |

Usa `alembic revision --autogenerate` y valida contra la base real: hay Postgres vivo.

### Construido

`app/config.py`, `app/db.py`, `app/errors.py` · `app/models.py` con las 7 tablas ·
migraciones aplicadas · `app/interoperabilidad/` (cliente del centralizador, cliente de
otros operadores, bandeja de salida, recepción de transferencias) ·
`app/mock/registraduria.py` · `app/identidad/` (registro, correo, seguridad, sesión,
token, TOTP, dependencias) · `app/documentos/` (almacenamiento S3, detección de tipo,
rutas, autenticación) · `scripts/limpiar_prueba.py` · `scripts/probar_transferencia.py`.

**Los cuatro flujos obligatorios de la entrega están implementados y probados de punta
a punta contra el sistema real del MinTIC.**

- **CU-01, registro del ciudadano**: A1, A2 y E1 a E6. El ciudadano queda afiliado a
  ColCarpeta en `validateCitizen`.
- **CU-02, inicio de sesión**: A1, A2 y E1 a E4. JWT RS256, segundo factor en modo
  `simulado`, bloqueo por intentos derivado de `auditoria`. `DELETE /api/v1/sesion` no
  revoca el token: vence solo a los 30 minutos.
- **CU-05, carga de documentos**: A2 y E1 a E5. La sustitución es explícita, por el
  campo `sustituye_a`.
- **CU-11, autenticación ante GovCarpeta**: A1, A2 y E1 a E5. El centralizador descarga
  el documento del bucket por el enlace firmado y responde 200.

**CU-16, recepción de transferencias, lado receptor implementado y probado de punta a
punta** (`POST /api/transferCitizen`, `POST /api/transferCitizenConfirm`): descarga de
documentos, validación de límites, `citizenEmail` adoptado como `email_carpeta` (AD-10),
`validateCitizen`/`registerCitizen` y `confirmAPI` por bandeja de salida. La verificación
de origen en `transferCitizenConfirm` es heurística (IP contra el host de
`transfer_api_url` en `operador_cache`): el ecosistema no tiene autenticación real.
La descarga de documentos de otro operador (`app/interoperabilidad/operadores.py`) es en
streaming y aplica el límite de tamaño sobre los bytes que realmente llegan, nunca sobre
el `Content-Length` que declara el remoto (puede mentir o no venir). **El lado emisor
(enviar un ciudadano a otro operador) no está implementado**: no hay código que llame
`unregisterCitizen` + `POST /api/transferCitizen` de un destino, así que hoy nunca se
crea una fila `transferencia` en estado `ENVIADA` de forma orgánica.

**Bandeja de salida: recuperación de filas colgadas.** Una fila que quedó en
`EN_PROCESO` (el proceso que la tomó murió, se colgó, o hubo un redespliegue a mitad de
ejecución) se revive sola pasados `OUTBOX_EN_PROCESO_MAXIMO_SEGUNDOS` (900 s por
defecto): vuelve a `PENDIENTE` contando como un intento más, con auditoría propia
(`outbox.colgada_recuperada`) independiente de si además llegó a un estado terminal. Ver
`app.interoperabilidad.outbox._recuperar_colgadas`. Probado de punta a punta (incluida
la reutilización del efecto de descarte de CU-16 al agotar reintentos) el 2026-09-21,
tanto en aislamiento como dentro de un contenedor Linux real (ver más abajo).

**Sobre el cuelgue de Windows investigado en la sesión anterior: sigue sin confirmarse,
es una hipótesis, no una conclusión.** La sospecha (una limitación de `ProactorEventLoop`
al cancelar I/O de socket superpuesta) nunca se descartó de una causa distinta, y buena
parte de esas pruebas tuvo dos procesos de uvicorn compitiendo por las mismas filas de
outbox. Al probar el camino de descarga (CU-16) dentro de un contenedor Linux real en
esta misma máquina (`docker-compose.test.yml`, ver "Estructura"), la descarga del
documento y las llamadas a GovCarpeta y al centralizador respondieron en menos de un
segundo cada una, sin ningún colgón — pero eso tampoco descarta el problema en Windows,
solo dice que en Linux, en esa corrida puntual, no se reprodujo. Esa misma prueba en
Docker sí encontró un bug real y distinto: una fila `receiveTransferCitizen` quedó
`EN_PROCESO` con `tomado_en` en NULL (nunca debería pasar; `_reclamar_uno` pone ambos
campos en el mismo commit) y por eso invisible para `_recuperar_colgadas`, que comparaba
`tomado_en < limite_tiempo` — NULL en SQL nunca es menor que nada. Ya está corregido:
la consulta ahora trata NULL como "colgada" también. La causa raíz de cómo esa fila
llegó a ese estado sigue sin determinarse: en la misma corrida volvió a pasarle a la
MISMA fila una segunda vez, tras ser revivida y reclamada de nuevo con normalidad
(la segunda vez sí terminó bien, en un estado terminal estable). No se aisló si es un
problema real de `_reclamar_uno`/asyncpg bajo concurrencia real (candidato: el mismo
tipo de comportamiento de "insertmanyvalues" de asyncpg que ya causó un problema
distinto con inserciones de `auditoria` en esta sesión, aplicado esta vez a un UPDATE)
o un artefacto de las pruebas manuales hechas en paralelo sobre la misma base. La
mitigación (tratar NULL como colgada) hace que el sistema se recupere solo de todas
formas, pero si vuelve a aparecer vale la pena investigarlo con más cuidado antes de
asumir que está resuelto.

### Pendiente

**CU-09, validación de firma digital** (A1 de CU-05 y CU-16): sin implementar.
`documento.firma_valida` queda siempre en nulo. Requiere `pyHanko` para validar firmas
PAdES dentro del PDF.

**Envío de transferencias** (lado emisor de CU-16, ver arriba), la purga física periódica
de transferencias `CONFIRMADA` (hoy solo se agenda `purgar_despues_de`, nada la ejecuta),
notificaciones más allá del correo de registro, consola de administración.

No registrar `registerTransferEndPoint` todavía: ver "Prohibido".

## Identidad del operador

| Dato                        | Valor                                                |
| --------------------------- | ---------------------------------------------------- |
| Nombre                      | ColCarpeta                                           |
| `operatorId` ante el MinTIC | `6aaeb415b765590002607402`                           |
| API del centralizador       | `https://govcarpeta-apis-4905ff3c005b.herokuapp.com` |

## Stack

Python 3.12 · FastAPI sobre Uvicorn · SQLAlchemy 2 async · Alembic · httpx ·
PostgreSQL y almacenamiento S3 en Supabase · despliegue en Railway.

## Estructura

```
app/
  main.py                      arranque, middleware de correlación, ciclo de vida
  config.py  db.py  errors.py  configuración, sesión de BD, sobre de errores
  models.py                    las 7 tablas
  interoperabilidad/
    govcarpeta.py              ÚNICO cliente del centralizador
    operadores.py              cliente de otros operadores (CU-16: descarga, confirmAPI)
    outbox.py                  proceso de bandeja de salida
    transferencias.py          POST /api/transferCitizen y /transferCitizenConfirm
  mock/registraduria.py        Registraduría simulada
alembic/versions/              migraciones
docs/especificacion.md         la especificación completa
scripts/probar_govcarpeta.py   prueba de humo contra la API real
scripts/limpiar_prueba.py      borra toda huella local y en el centralizador de una cedula
scripts/probar_transferencia.py  simula un operador de origen enviando CU-16
Dockerfile                     imagen de la app; la usa Railway Y docker-compose.test.yml
docker-compose.test.yml        solo para probar en Linux en esta maquina (Docker Desktop),
                                nunca para desplegar -- ver "Probar en Linux" mas abajo
```

## Arquitectura en una frase

Seis componentes lógicos (Gateway, Identidad, Documentos, Interoperabilidad,
Notificaciones, Administración y Auditoría) desplegados como módulos de **una sola**
aplicación. `app/interoperabilidad/` es el único módulo que habla con el centralizador
y con otros operadores; ningún otro módulo debe importar `govcarpeta.py`.

## Trampas del contrato del centralizador (verificadas contra el servicio real)

Estas no están en la documentación del MinTIC y rompen el sistema en silencio:

1. **`GET /apis/validateCitizen/{id}` tiene la semántica invertida.**
   `204` = el ciudadano NO está afiliado, se puede registrar.
   `200` = el ciudadano YA está afiliado a otro operador, se rechaza el registro.
   Decidir siempre por el código HTTP, nunca por el cuerpo.
2. **`DELETE /apis/unregisterCitizen` lleva cuerpo JSON.** En httpx hay que usar
   `client.request("DELETE", url, json=...)`; el atajo `client.delete()` no admite cuerpo.
3. **`authenticateDocument` usa `UrlDocument` con U mayúscula inicial.**
   El centralizador descarga esa URL, así que debe ser alcanzable sin credenciales:
   se envía siempre un enlace firmado con vigencia corta, nunca un objeto público.
4. **`getOperators` no devuelve el endpoint de confirmación** aunque
   `registerTransferEndPoint` permita registrarlo. Por eso la URL de confirmación viaja
   dentro del cuerpo de `transferCitizen`, según el acuerdo entre los equipos del curso.
5. **El directorio es dato sucio**: nombres de operador duplicados, URLs con espacios al
   inicio, URLs sin TLS, y solo ~16 de 73 operadores publican `transferAPIURL`.
   Recortar espacios, resolver por `_id` y nunca por `operatorName`.
6. **La API del centralizador no tiene autenticación de ningún tipo.** No sirve como
   ancla de confianza: los endpoints de transferencia entrantes llevan controles propios.

## Reglas de implementación que no se negocian

- **El centralizador no va en la ruta crítica.** Solo `validateCitizen` es síncrona.
  Todo lo demás se escribe en la tabla `outbox` dentro de la misma transacción de
  negocio y lo ejecuta el proceso en segundo plano, con reintentos y espera exponencial.
- **La aplicación no escribe en disco local.** Todo el estado vive en PostgreSQL y en el
  bucket. Es lo que permite correr varias réplicas.
- **Borrado diferido**: nada se borra físicamente en el momento; se marca y se purga
  después de `PURGE_DELAY_DAYS`.
- **Toda operación relevante deja registro en `auditoria`**, que es de solo inserción
  (hay listeners de SQLAlchemy que bloquean UPDATE y DELETE sobre esa tabla).
- **Ningún secreto en el repositorio.** Solo `.env.example` con los nombres.
- **`email_carpeta` es el identificador permanente del ciudadano, no un buzón** (AD-10).
  Se genera una sola vez, en el registro. Al transferir se envía en `citizenEmail` y el
  correo personal viaja en la extensión `contactEmail`. Al recibir un ciudadano se adopta
  el `citizenEmail` que llega, sea cual sea su dominio, y **no** se genera una dirección
  propia. La resolución de colisiones del patrón solo aplica a direcciones propias.

## Prohibido

- **No invocar `registerTransferEndPoint` contra el MinTIC** hasta que
  `/api/transferCitizen` y `/api/transferCitizenConfirm` funcionen de verdad. El
  directorio es público y lo consultan 72 equipos más: publicar rutas que no responden
  hace que otros operadores envíen ciudadanos que se pierden.
- **No llamar `registerCitizen` ni `unregisterCitizen` con cédulas inventadas** para
  "probar". El directorio del MinTIC es compartido y esos registros quedan permanentes.
  Para probar, usar `validateCitizen` y `getOperators`, que son de solo lectura.
- **No exponer objetos del bucket de forma pública.** El único acceso de terceros es un
  enlace firmado con vigencia.
- **No matar procesos que no hayas iniciado tú en esta sesión.** El puerto 8000
  normalmente tiene el servidor de desarrollo del usuario corriendo con `--reload`.
  Si necesitas un servidor para probar, levanta uno en un puerto propio y apágalo
  al terminar.
- **Cualquier servidor vivo (incluido el tuyo, en tu propio puerto) tiene su propia
  bandeja de salida corriendo cada `OUTBOX_INTERVALO_SEGUNDOS`.** Si una prueba escribe
  una fila de `outbox` con `registerCitizen` o `unregisterCitizen` (aunque sea indirecta,
  p. ej. al probar la recuperación de CU-16 con `req_status = 0`), ese servidor la va a
  procesar de verdad en segundos — borrarla "a tiempo" es una carrera que se pierde
  fácilmente. Para probar esos caminos, inyecta un `GovCarpeta` falso directamente en
  `procesar_lote(...)` en vez de dejar que un servidor vivo la tome.

## Decisiones cerradas — no reabrir

Están argumentadas en `docs/especificacion.md`, sección "Decisiones de arquitectura", y
sostienen el documento que se entrega. Cambiarlas obliga a corregir la entrega.

- Sin broker de mensajería: la bandeja de salida es una tabla de PostgreSQL.
- Una sola unidad desplegable; la separación de `interoperabilidad` es la arquitectura
  objetivo, no lo que se despliega hoy.
- Identidad propia con JWT y TOTP; se descartó Keycloak.
- El segundo factor corre en modo `simulado` (`TOTP_MODO`): el flujo completo existe y
  solo la verificación del código está parametrizada.
- La cuenta de correo del ciudadano se elimina cuando se traslada a otro operador.
- El formato de transferencia entre operadores se respeta al pie de la letra; las
  mejoras propias son aditivas y los controles son unilaterales.

## Comandos

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload                 # desarrollo
alembic revision --autogenerate -m "mensaje"  # nueva migración
alembic upgrade head                          # aplicar migraciones
python scripts/probar_govcarpeta.py           # prueba de humo contra la API real
```

## Cómo probar los caminos de excepción

La Registraduría simulada responde según el **último dígito de la cédula**:

| Termina en     | Respuesta                    | Prueba        |
| -------------- | ---------------------------- | ------------- |
| 0              | 404, identidad no confirmada | E2 de CU-01   |
| 9              | demora 35 s y luego 504      | E3 de CU-01   |
| cualquier otro | 200, identidad confirmada    | camino básico |

Para el centralizador: la cédula `1234567890` ya está afiliada a otro operador, así que
`validateCitizen` devuelve 200 y sirve para probar E1 de CU-01. Es también la cédula por
defecto de `scripts/probar_transferencia.py`: al estar afiliada a otro operador, CU-16
falla en `validateCitizen` antes de llegar a `registerCitizen`, así que ejercita casi
toda la recepción (descarga, límites, normalización, `confirmAPI` con `req_status = 0`)
sin registrar nada de verdad.

## Probar en Linux (Docker Desktop)

Para verificar en Linux el camino de descarga de CU-16 sin exponer nada y sin depender
de Railway (que no puede alcanzar el servidor de confirmación local que levanta
`scripts/probar_transferencia.py`): `docker-compose.test.yml` levanta la app real (con
el `.env` local montado) y el script en dos contenedores separados, en una red Docker
aislada -- nada se publica salvo el puerto 8000 de la app, igual que en producción.

```bash
docker compose -f docker-compose.test.yml up --build -d app   # la app, en Linux real
docker compose -f docker-compose.test.yml logs -f app          # ver el worker de outbox
docker compose -f docker-compose.test.yml up prueba-transferencia   # corre el script (con "up", no "run": ver el comentario en el archivo)
docker compose -f docker-compose.test.yml down                 # apaga y limpia la red
```

Usa la misma base de datos y bucket reales de Supabase que `uvicorn --reload` en
Windows -- no es una base de pruebas aparte. Se probó el 2026-09-21 con la cédula segura
1234567890: la descarga del documento, `validateCitizen` y `confirmAPI` respondieron
todos en menos de un segundo dentro del contenedor. Ver la nota sobre el cuelgue de
Windows más arriba para lo que esto sí y no demuestra.

## Convenciones

- Nombres de dominio en español (`ciudadano`, `documento`, `outbox`), código en inglés
  donde sea idiomático de la librería.
- Errores de la API propia siempre con el sobre `{"error": {...}}` de `app/errors.py`.
  Los endpoints de transferencia entre operadores NO usan ese formato: responden con lo
  que define el acuerdo del ecosistema, para no romper a los demás operadores.
- Un módulo por componente lógico bajo `app/`. Las fronteras entre módulos se respetan
  aunque hoy compartan proceso: la arquitectura objetivo separa `interoperabilidad`.
- Cuando termines una parte, actualiza la sección **Estado actual** de este archivo.
