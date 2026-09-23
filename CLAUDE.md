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

`app/config.py`, `app/db.py`, `app/errors.py` · `app/models.py` con las 9 tablas
(agregadas `notificacion` CU-17 y `entidad_emisora` CU-13) · migraciones aplicadas ·
`app/interoperabilidad/` (cliente del centralizador, cliente de otros operadores,
bandeja de salida, envío y recepción de transferencias) · `app/mock/registraduria.py` ·
`app/identidad/` (registro, correo, seguridad, sesión, token, TOTP, dependencias,
perfil) · `app/documentos/` (almacenamiento S3, detección de tipo, rutas, autenticación,
búsqueda, eliminación y sustitución de documentos, depósito por entidad emisora,
validación de firma digital con pyHanko) · `app/notificaciones/` (envío simulado y
centro de notificaciones, CU-17) · `scripts/limpiar_prueba.py` ·
`scripts/probar_transferencia.py` · `scripts/probar_envio_transferencia.py` ·
`scripts/mock_centralizador.py` · `scripts/probar_carpeta_completa.py` ·
`scripts/alta_entidad_emisora.py` · `scripts/probar_colision_email.py` ·
`scripts/probar_firma_digital.py` · `scripts/probar_firma_transferencia.py`.

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
el `Content-Length` que declara el remoto (puede mentir o no venir).

**CU-03, envío de un ciudadano a otro operador, implementado y probado de punta a
punta el 2026-09-22** entre dos instancias propias en Linux (ver "Probar en Linux" más
abajo) — nunca contra el directorio real del MinTIC. `POST /api/v1/perfil/traslado`
(nuevo, autenticado, sin segundo factor) valida `estado == ACTIVO` y que no haya ya una
transferencia `ENVIADA`, resuelve el destino por `_id` contra el `operador_cache` que
ya haya (sin llamar al centralizador en la ruta), marca al ciudadano `EN_TRANSFERENCIA`
y encola `enviarTransferencia`. Ese manejador de outbox (`app.interoperabilidad.outbox`)
hace el refresco "a demanda" de verdad, genera enlaces de 24 h, invoca
`unregisterCitizen`, hace `POST /api/transferCitizen` al destino y marca `ENVIADA`
(crea la fila `transferencia`). La confirmación (`POST /api/transferCitizenConfirm`,
que recibe el propio ColCarpeta como emisor) ya estaba construida desde la sesión de
CU-16: marca `CONFIRMADA` + programa la purga, o recupera con `registerCitizen` si
`req_status = 0` — CU-03 solo le dio un emisor real. Si el envío mismo no se completa
(límites, red agotada, directorio sin URL utilizable), `_al_finalizar_envio_transferencia`
recupera al ciudadano exactamente igual que un `req_status = 0` real; probado (por
accidente, ver más abajo) end-to-end.

Además, cada ciclo del bucle de fondo hace mantenimiento periódico (cada
`DIRECTORIO_OPERADORES_REFRESCO_SEGUNDOS`, 900 s por defecto): refresca
`operador_cache` desde `getOperators`, **reconcilia transferencias `ENVIADA` más viejas
que `TRANSFER_CONFIRM_TIMEOUT`** sin confirmación (consulta `validateCitizen` en vez de
dejarlas colgadas para siempre), y **purga físicamente** las `CONFIRMADA` cuyo
`purgar_despues_de` ya pasó (borra documentos, objetos del bucket y el ciudadano; la
fila `transferencia` sobrevive como `PURGADA`, con `ciudadano_id` en NULL). La consulta
exacta de la purga (es el único código del proyecto que destruye datos, y corre contra
la base real): `estado == CONFIRMADA AND purgar_despues_de IS NOT NULL AND
purgar_despues_de <= ahora`, las tres condiciones exigidas a la vez
(`app.interoperabilidad.outbox._purgar_transferencias`), con una relectura bajo
`FOR UPDATE` antes de borrar nada para no chocar con una confirmación concurrente.

**Purga y reconciliación probadas de punta a punta el 2026-09-22**
(`scripts/probar_reconciliacion.py`, dentro de `docker-compose.test.yml`): sin esperar
horas ni manipular el reloj del sistema, se inserta directamente una fila
`transferencia` `ENVIADA` con `enviada_en` retrocedido en la base más allá de
`TRANSFER_CONFIRM_TIMEOUT`, y se corre `_reconciliar_transferencias` una sola vez.
Cubre los tres desenlaces: `validateCitizen` confirma el destino (→ `CONFIRMADA` + purga
programada, igual que `req_status=1`), `validateCitizen` dice disponible (→ `FALLIDA` +
`registerCitizen` reencolado, igual que `req_status=0`), y el caso ambiguo (200 pero sin
nombrar al destino: se queda `ENVIADA`, no se resuelve sola, y queda auditado como
`transferencia.reconciliacion_ambigua`). Los tres pasaron.

**Tres bugs reales encontrados y corregidos al probar CU-03 de punta a punta
(2026-09-22), ninguno hipotético:**

1. **`HEAD` sobre un enlace firmado de Supabase responde 403, aunque el mismo enlace
   responda 200 a `GET`.** `obtener_tamano()` (paso 1 de "Orden de recepción") trataba
   cualquier `HEAD` fallido como `OperadorNoDisponible` (reintentable indefinidamente):
   como el chequeo de tamaño es solo una optimización y `descargar()` ya aplica el
   límite de verdad en streaming, ahora un `HEAD` fallido simplemente devuelve tamaño
   desconocido (`None`) en vez de bloquear la recepción. Sin este arreglo, **ningún
   operador cuyo almacenamiento sea Supabase — incluido otro ColCarpeta — podría recibir
   nunca nada**, porque el emisor firma su propio enlace con su propio Supabase. No se
   había detectado antes porque las pruebas previas de CU-16 siempre usaban una URL
   externa real (w3.org), nunca un enlace firmado propio.
2. **`Transferencia.ciudadano_id` no admitía NULL y no tenía `ON DELETE SET NULL`.** La
   purga física borra al ciudadano pero debe conservar la fila `transferencia` como
   rastro (RNF14) — el mismo patrón ya usado en `auditoria`, que aquí faltaba. Sin el
   arreglo, la purga fallaba con una violación de FK. Corregido con migración
   `de098854465b` (aplicada a la base real) + `passive_deletes=True` en
   `Ciudadano.transferencias`.
3. **Colisión de `email_carpeta` al recibir.** Dos variantes, con desenlaces distintos:
   - **El mismo ciudadano que ya fue nuestro y vuelve antes de que se cumpla su propia
     purga diferida (resuelto, 2026-09-22).** Si alguien se trasladó a otro operador y
     su fila local sigue en `TRASLADADO` (todavía no pasó `PURGE_DELAY_DAYS`), y luego
     vuelve por una transferencia real, `citizenEmail` trae exactamente la misma
     dirección de siempre (AD-10: es portable y permanente) — chocaría contra su
     **propio** registro viejo, no contra el de otra persona. Decisión: ese registro
     viejo ya no hace falta conservarlo más allá de la purga diferida una vez que su
     salida quedó `CONFIRMADA` (la fila `transferencia`, que sobrevive a la purga, ya es
     el rastro permanente — no la fila `ciudadano`), así que se reemplaza de inmediato
     en vez de obligar a esperar los días que falten. Corregido en dos puntos que hay
     que tocar juntos: el chequeo de idempotencia de la propia ruta
     (`app/interoperabilidad/transferencias.py`, `_recibir_transferencia_impl` — sin
     esto ni siquiera se llega a encolar nada, la ruta descarta la transferencia como
     "duplicada" antes de que la bandeja de salida la vea) y el paso 2 del manejador de
     outbox (`_recibir_transferencia`, que borra el ciudadano/documentos/objetos viejos,
     dejando auditoría `transferencia.registro_anterior_reemplazado`, antes de crear la
     fila nueva). Solo aplica al estado `TRASLADADO` específicamente: cualquier otro
     estado ya existente para esa cédula (`ACTIVO`, `EN_TRANSFERENCIA`,
     `PENDIENTE_VERIFICACION`, `PENDIENTE_CENTRALIZADOR`) sigue tratándose como conflicto
     real y no se toca. Probado de punta a punta con
     `scripts/probar_regreso_antes_de_purga.py`: documento y objeto S3 viejos borrados,
     documento nuevo guardado, ciudadano llega a `ACTIVO` (no se queda colgado en
     `TRASLADADO`).
   - **Dos ciudadanos _distintos_, de operadores de origen distintos, que por
     coincidencia generan el mismo `citizenEmail` (mismo nombre y año) hacia el mismo
     destino (resuelto, 2026-09-22).** A diferencia de la variante anterior, aquí no hay
     un registro "propio" que reemplazar: son dos identidades reales distintas, y AD-10
     prohíbe inventarle una dirección alterna a cualquiera de las dos. Política
     documentada en docs/especificacion.md ("Interoperabilidad entre operadores" >
     "Colisión de `email_carpeta` entre dos ciudadanos distintos") y en AD-10: se
     rechaza la recepción entrante, nunca al ciudadano ya afiliado en ColCarpeta.
     Corregido en `_recibir_transferencia` (paso 2): antes de crear el ciudadano,
     consulta si `email_carpeta` ya pertenece a una cédula distinta y, si es así,
     levanta un `ValueError` con el diagnóstico explícito (qué cédula ya tiene esa
     dirección) en vez de dejar que la violación de la restricción UNIQUE de la base
     lo tumbe con un error opaco. El efecto es el mismo que cualquier otro rechazo de
     esta recepción: `req_status = 0`, el origen recupera al ciudadano con su propio
     `registerCitizen`. Probado de punta a punta con
     `scripts/probar_colision_email.py`: dos cédulas distintas, mismo `citizenEmail`,
     la segunda se rechaza con el diagnóstico correcto y la primera queda intacta.

**Primer acceso: establecimiento de contraseña para un ciudadano recibido por
transferencia, implementado y probado de punta a punta el 2026-09-22.** Ese ciudadano
llega sin `password_hash` y no podía iniciar sesión hasta ahora. Al completar la
recepción con éxito, si trajo `contactEmail` (`email_personal`),
`_emitir_token_primer_acceso` (`app/interoperabilidad/outbox.py`) genera un token de un
solo uso (`app/identidad/token_acceso.py`: aleatorio de 256 bits, hasheado con SHA-256
-- determinista a propósito, para poder buscarlo por igualdad; a diferencia de una
contraseña no hace falta un hash costoso porque el valor ya tiene entropía alta) y lo
"envía" por `app/notificaciones/correo.py`. Si no trajo `contactEmail`, no se inventa un
canal alterno: el ciudadano queda con `password_hash` en NULL (pendiente de primer
acceso) y una auditoría `primer_acceso.sin_canal` explica por qué.

`POST /api/v1/primer-acceso` (nueva, sin sesión -- el ciudadano todavía no puede
autenticarse) recibe `{token, password}`, exige las mismas reglas de contraseña que el
registro (compartidas ahora en `app/identidad/seguridad.py`, antes solo en
`registro.py`), y responde siempre el mismo `400 TOKEN_INVALIDO` sin distinguir "no
existe", "ya se usó" o "venció" -- nunca revela si el token corresponde a una cédula
real. Las tres auditorías pedidas están cubiertas con acciones propias:
`primer_acceso.token_emitido`, `primer_acceso.token_usado`,
`primer_acceso.token_vencido` (más `primer_acceso.token_invalido` para un token que
nunca existió o que ya se consumió antes). El token en claro nunca se persiste en
ningún lado, ni siquiera en el detalle de la auditoría.

**No existía ningún mecanismo de notificación en el proyecto antes de esto** --ni
siquiera el "correo de registro" que el estado de la implementación daba por hecho
(`docs/especificacion.md`, paso 9 de "Registro del ciudadano"): `app/identidad/correo.py`
solo generaba la dirección `email_carpeta`, nunca enviaba nada. `app/notificaciones/`
es hoy el único lugar que "envía" algo, y lo hace simulado (sin proveedor real
integrado, igual que la Registraduría o `TOTP_MODO=simulado`): se registra con
`logger.info` para poder demostrarlo. Esto exigió un arreglo aparte: el proyecto nunca
había llamado a `logging.basicConfig`, así que ese `logger.info` no aparecía en ningún
lado (sin handlers configurados, Python solo aplica su manejador de último recurso, que
filtra en WARNING). `app/main.py` ahora configura el nivel global en WARNING y sube
`"colcarpeta"` (el prefijo de todos los logueadores propios) a INFO.

**Correo de registro (CU-01, paso 9) conectado, y reenvío del token de primer acceso
implementado, ambos probados de punta a punta el 2026-09-22.** El correo de registro
faltaba por completo (ver el hallazgo de la pasada anterior) aunque `registro.py` no lo
mencionaba: quedó pendiente sin decirlo. Ahora `_activar_ciudadano`
(`app/interoperabilidad/outbox.py`) envía la notificación al pasar a `ACTIVO`, pero
**solo** para un registro real -- `registro.py` marca el payload con
`notificar_registro: True` al encolar `registerCitizen`, porque ese mismo manejador
también reactiva a un ciudadano recuperado tras un envío o una recepción fallidos
(CU-03/CU-16), y ahí no correspondería un correo de "bienvenida". Auditado como
`registro.notificacion_enviada`.

`POST /api/v1/primer-acceso/reenviar` (nueva, pública, sin sesión) resuelve al
ciudadano por cédula o `email_carpeta` (comparte `resolver_usuario`, ahora en
`app/identidad/dependencias.py`, con el login) y responde **siempre el mismo mensaje
fijo**, exista o no la carpeta y esté o no pendiente de primer acceso. Si de verdad
está pendiente y tiene `email_personal`, genera un token nuevo (sobrescribe la fila:
el anterior queda inservible de inmediato, sin borrado aparte) y lo envía; si no está
pendiente, si no existe, o si no tiene `email_personal`, no hace nada -- en los tres
casos, la misma respuesta. Limitado por `PRIMER_ACCESO_REENVIO_MAXIMO` (3 por defecto)
dentro de `PRIMER_ACCESO_REENVIO_VENTANA_MINUTOS` (15), por cédula y por origen, igual
patrón que el bloqueo de inicio de sesión (derivado de `auditoria`, sin tabla propia) --
las solicitudes ignoradas y limitadas también cuentan para el límite por origen, para
que enumerar cédulas al azar no lo esquive. Tres acciones de auditoría cubren "atendidas,
ignoradas y limitadas": `primer_acceso.reenvio_atendido`, `.reenvio_ignorado`,
`.reenvio_limitado`. Probado de punta a punta con
`scripts/probar_reenvio_primer_acceso.py` (token vencido → reenvío → token viejo sigue
sin servir → token nuevo funciona → login) más verificación manual de las ramas
ignorada (ciudadano ya activo, cédula inexistente) y limitada.

`TRANSFERENCIA_EXIGIR_HTTPS` (nueva variable, `true` por defecto): CU-03 rechaza un
destino cuyo `transferAPIURL` no sea `https://`. Se puede apagar solo para pruebas
locales entre instancias propias sin TLS (`docker-compose.test.yml`) — **nunca en
`.env` real ni en Railway**.

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

**CU-07 (buscar y clasificar), CU-08 (eliminar documento no certificado), CU-10
(sustituir documento temporal) y CU-17 (centro de notificaciones), más `GET`/`PATCH
/api/v1/perfil`, implementados y probados de punta a punta el 2026-09-22.** Ninguno de
los cuatro casos de uso tiene especificación detallada en el documento (solo CU-01,
CU-02, CU-05 y CU-11 la tienen), así que varias decisiones de forma quedaron a criterio
de esta implementación, documentadas abajo.

`documento` gana un estado propio (`estado`: `ACTIVO` | `REEMPLAZADO` | `ELIMINADO`,
antes no existía) que unifica CU-08 y CU-10 sin perder información:

- **CU-10 dejó de borrar el documento sustituido.** Antes (A2 de CU-05), sustituir un
  documento con `sustituye_a` borraba la fila y el objeto del anterior de inmediato —
  funcionaba, pero le faltaba justo lo que pide CU-10 explícitamente ("sin perder su
  historia"). Ahora el documento viejo pasa a `REEMPLAZADO` (fila y objeto en el bucket
  se conservan indefinidamente) y el nuevo queda enlazado a él por `sustituye_a_id`. Uno
  `REEMPLAZADO` deja de aparecer en el listado (`GET /documentos`) y de contar contra la
  cuota, pero sigue siendo consultable por `GET /documentos/{id}` para seguir el
  historial hacia atrás. No se agregó una forma de marcar `certificado=true`: sigue sin
  existir un camino real hacia ahí en esta entrega (ver CU-09, más abajo) — la prueba de
  punta a punta sustituye un documento temporal por otro documento temporal, no por uno
  certificado de verdad.
- **CU-08 es borrado diferido, igual patrón que `transferencia`.** `DELETE
  /api/v1/documentos/{id}` marca `ELIMINADO` con `purgar_despues_de` (`PURGE_DELAY_DAYS`
  después) en vez de borrar nada en el momento; rechaza documentos certificados (409) y
  documentos ya `REEMPLAZADO` (409, no tiene sentido "eliminar" algo que ya dejó de estar
  vigente por otra vía). `app.interoperabilidad.outbox._purgar_documentos`, nueva parada
  del mismo mantenimiento periódico que ya purgaba `transferencia`, borra la fila y el
  objeto del bucket al cumplirse el plazo. Esto exigió una corrección de modelo
  relacionada: `autorizacion.documento_id` no tenía `ON DELETE CASCADE` — sin eso, la
  purga fallaría por violación de FK en cuanto existiera alguna autorización sobre el
  documento (CU-18 sigue sin implementar, así que hoy nunca pasa, pero habría sido el
  mismo tipo de bug que ya costó una migración aparte con `transferencia.ciudadano_id`).

CU-07 extiende `GET /api/v1/documentos` (ya tenía `tipo`, `entidad`, `desde`, `hasta`,
`q`, paginación) con `certificado` y `estado_autenticacion`, y ahora excluye por defecto
lo `REEMPLAZADO`/`ELIMINADO` — solo se listan documentos vigentes.

CU-17 se apoya enteramente en `app/notificaciones/`, sin mecanismo paralelo:
`enviar_correo` (antes solo registraba en el log) ahora también inserta una fila en la
nueva tabla `notificacion`, así que cada correo simulado que el sistema ya enviaba
(registro, primer acceso, reenvío) queda además en la bandeja del ciudadano. Esto
cambió su firma (ahora recibe la sesión y `ciudadano_id`) y se actualizaron los tres
puntos que la llaman. `GET /api/v1/notificaciones` (paginado, con `solo_no_leidas`) y
`POST /notificaciones/{id}/leida` son las dos rutas nuevas.

`GET/PATCH /api/v1/perfil` (nuevo archivo `app/identidad/perfil.py`): `GET` devuelve los
datos del ciudadano, el estado de la carpeta y la cuota (`cuota_bytes`/`usado_bytes`,
mismo cálculo que la carga de documentos). `PATCH` solo admite `direccion`, `telefono` y
`email_personal` — ni la cédula ni `email_carpeta` son editables (AD-10).

**CU-11 ya no deja pedir autenticación de un documento fuera de vigencia, probado el
2026-09-22.** `POST /documentos/{id}/autenticacion` sobre un documento `REEMPLAZADO`
ahora responde 409 `ESTADO_INVALIDO` (no tiene sentido reenviar al centralizador algo
que el ciudadano ya sustituyó), y sobre uno `ELIMINADO` sigue respondiendo 404 —eso ya
lo cubría `_obtener_propio` desde CU-08, sin que nadie lo hubiera probado explícitamente
hasta ahora—. `GET .../autenticacion` no cambió: sigue mostrando el resultado guardado
de un documento `REEMPLAZADO`, porque ahí sí aplica "sin perder su historia".

**CU-13 (mínimo), depósito de un documento certificado por una entidad emisora,
implementado y probado de punta a punta el 2026-09-22.** Es la primera vía real por la
que un documento llega a `certificado = true` — hasta ahora esa condición nunca se
ejercitaba (ver el punto de "Pendiente" que esto resuelve, abajo). Nueva tabla
`entidad_emisora` (id = identificación tipo NIT, nombre, `api_key_hash`, `estado`) — es
CU-04 en su mínima expresión, solo lo que CU-13 necesita: no hay registro público ni
consola, solo `scripts/alta_entidad_emisora.py` (acceso directo a la base). `POST
/api/v1/entidades/documentos` (nuevo módulo `app/documentos/entidades.py`) autentica a
la entidad por la clave de API en `X-Api-Key` (mismo esquema de hash que el token de
primer acceso, generalizado en `app.identidad.token_acceso`), exige que la cédula
corresponda a un ciudadano `ACTIVO` (404 si no), y crea el documento con
`certificado = true`, `entidad_emisora` tomado del nombre de la entidad autenticada
(nunca de un campo declarado en la petición), sin consumir cuota. Admite `sustituye_a`
igual que la carga propia del ciudadano (CU-10), reutilizando la misma validación
(`resolver_sustitucion`, extraída de `documentos/router.py` para compartirla entre los
dos módulos). El ciudadano recibe notificación por el centro de CU-17. CU-09
(validación de firma, ver más abajo) se ejecuta igual que en CU-05 si el archivo
depositado es un PDF.

**Revocación y reactivación de una entidad emisora, agregadas el 2026-09-22.**
`EntidadEmisora.estado` (`ACTIVA` | `REVOCADA`) se agregó específicamente porque antes
solo se podía rotar la clave, nunca desactivar una entidad sin borrar la fila —y
borrarla habría roto su rastro en `auditoria` y el `entidad_emisora` (texto plano) ya
guardado en los documentos que depositó—. `scripts/alta_entidad_emisora.py` ahora tiene
tres subcomandos: `alta` (crea o rota la clave, sin tocar el estado), `revocar` y
`reactivar`. `app.documentos.entidades.entidad_actual` rechaza con 401 a una entidad
`REVOCADA` aunque presente la clave correcta; revocar no invalida esa clave, solo
bloquea su uso, así que reactivar la deja funcionando de nuevo sin necesidad de
comunicarle una clave nueva.

Probado de punta a punta con `scripts/probar_carpeta_completa.py` (contra `app-a`
viva), ahora extendido: filtros de CU-07, sustitución CU-10 con verificación de que el
documento viejo sigue consultable y ya no cuenta en la cuota, borrado CU-08 con sus
rechazos (409 certificado, 409 ya reemplazado), CU-11 rechazando autenticación sobre lo
no vigente, el depósito de CU-13 (certificado, entidad tomada de la credencial, sin
impacto en cuota, sustituyendo un temporal, sin admitir borrado), la entidad revocada
rechazada con 401 y reactivada con la misma clave, notificación mencionando a la
entidad y la de registro marcable como leída en CU-17, y `PATCH /perfil` aplicando los
cambios sin tocar `email_carpeta`. Al final prueba también `_purgar_documentos` en
aislamiento (retrocede `purgar_despues_de` en la base, sin esperar `PURGE_DELAY_DAYS`
de verdad), igual patrón que `scripts/probar_reconciliacion.py`.

**CU-09, validación de firma digital, implementada y probada de punta a punta el
2026-09-22 en CU-05 y CU-13** (CU-16 se enganchó aparte el mismo día, ver más abajo --
juntas cierran el último pendiente declarado del entregable). Nuevo módulo
`app/documentos/firma.py`, con `pyHanko`: valida que el contenido de un PDF no cambió
después de firmarse (`intact`), que la firma en sí es criptográficamente correcta
contra la llave pública del certificado firmante (`valid`), y que esa firma cubre el
archivo completo (`coverage == ENTIRE_FILE`) — `firma_valida` es la conjunción de las
tres, evaluada solo sobre la última firma si el PDF trae más de una (este proyecto no
tiene flujo de cofirma). Extrae además `firma_firmante` (el sujeto del certificado) y
`firma_fecha` (la fecha que la propia firma reporta), ambos nuevos en `documento`.

**Deliberadamente NO valida la cadena de confianza contra ninguna autoridad
certificadora** (documentado en docs/especificacion.md, "Validación de firma digital
(CU-09)"): en Colombia eso exige el almacén de confianza de las entidades acreditadas
por la ONAC, que este proyecto no tiene, y afirmarlo sin tenerlo simularía una garantía
que no existe. Se le pasa siempre a pyHanko un `ValidationContext` sin raíces de
confianza y sin permiso de red (`trust_roots=[]`, `allow_fetching=False`) -- sin esto,
la versión de pyHanko usada (0.37.0) cae en su comportamiento por defecto (deprecado)
de validar contra el almacén de confianza **del sistema operativo**, que además de ser
el almacén equivocado haría el resultado depender de la máquina donde corre el proceso
y podría intentar red. Sin ese contexto explícito, pyHanko también registra un
`WARNING` por cada validación (no puede construir una ruta de confianza, algo
esperado y permanente en nuestro caso, no una anomalía); `app/main.py` sube el logger
`"pyhanko"` a `ERROR` para no ahogar los logs reales con eso.

Por AD-05 ("la validación de firmas... se ejecuta en el proceso de segundo plano y no
en la petición del ciudadano"), la validación nunca corre dentro de la petición: CU-05
y CU-13 solo encolan `validarFirma` en la misma transacción que crea el documento
(nunca para `image/jpeg` ni `image/png`, que no pueden traer una firma PAdES), y
`app.interoperabilidad.outbox._validar_firma` la ejecuta en segundo plano, descargando
el objeto del bucket (nueva `almacenamiento.descargar_objeto`, distinta de generar un
enlace: aquí hace falta el contenido real, no solo una URL). Una firma inválida
**nunca** es un rechazo: el documento se guarda igual, con el resultado visible --
`_validar_firma` no lanza una excepción de negocio por eso. `firma_valida` sin valor
(`NULL`) significa "no hay firma que validar, o la validación no ha corrido todavía";
`false` significa "sí se validó y no es válida" -- son estados distintos y la API
nunca los confunde.

Esto también resuelve el hueco de procedencia de metadatos que las tareas anteriores
habían dejado pendiente: `documento.certificado` (CU-13, la entidad autenticada
respalda `entidad_emisora`) y `documento.firma_valida` (CU-09, el contenido y el
firmante están respaldados criptográficamente) son ahora dos señales reales e
independientes que la API expone tal cual -- sin inventar una etiqueta de "procedencia"
propia, esa decisión de presentación es del portal, no del backend
(docs/especificacion.md, "Procedencia de los metadatos de un documento").

**CU-09 enganchado también en CU-16, el 2026-09-22.** Paso 3 de
`_recibir_transferencia` ("Orden de recepción")
encola `validarFirma` por cada documento PDF recibido, una fila de outbox por
documento (nunca todas de un tirón: con hasta 200 documentos por transferencia, abrir y
revisar cada PDF completo en la misma llamada que recibe al ciudadano competiría por
CPU con el resto de la bandeja de salida). Esto reemplaza la afirmación original de la
especificación ("los documentos recibidos entran en cuarentena hasta validar su firma")
por lo que el sistema decide hacer de verdad:

- El documento queda visible, listado y descargable desde el momento en que se crea --
  igual que en CU-05/CU-13. No hay un estado que lo oculte mientras se valida.
- Si la firma resulta inválida y el documento llegó marcado `certificado = true` por el
  **operador de origen** (una afirmación de un tercero sin autenticar -- CLAUDE.md,
  "trampa 6" -- cualitativamente más débil que la de CU-13, donde certifica una entidad
  autenticada directamente con nosotros), se le retira esa condición:
  `documento.certificado` pasa a `false`, con una auditoría propia
  (`documento.certificacion_revocada_por_firma_invalida`). Es la única consecuencia
  real de "cuarentena" en este sistema.
- Si el documento no trae ninguna firma (`firma_valida` sigue en NULL, no en `false`),
  el `certificado` declarado no se toca: no tener firma embebida no es sospechoso por
  sí solo -- CU-13 ya acepta esa misma combinación como normal.
- Esto nunca aplica a CU-13: ahí `certificado` lo otorga la entidad autenticada
  directamente, no una firma embebida ni la palabra de un tercero.

Documentado en docs/especificacion.md, nueva sección "Documentos recibidos por
transferencia (CU-16) y su firma" (reemplaza la mención suelta de "cuarentena" que
nunca se había cumplido).

**Dos detalles de esa revocación, cerrados el 2026-09-22 (el ciudadano no se enteraba, y
la cuota podía dispararse sin aviso):**

1. **Notificación por el centro de CU-17.** La revocación queda en `auditoria`, pero el
   ciudadano no puede consultar esa tabla -- sin avisarle por otro canal, nunca se
   entera de que un documento que veía certificado dejó de estarlo. `_aplicar_resultado_firma`
   ahora llama a `enviar_correo` (mismo mecanismo de siempre, sin canal paralelo) con un
   mensaje en lenguaje llano: qué documento, por qué (la firma no se pudo verificar,
   sin tecnicismos de pyHanko/PAdES) y qué cambia para él (ocupa cuota, se puede
   eliminar).
2. **Efecto en la cuota, decidido explícitamente en vez de descubierto por accidente.**
   Revisar el código confirmó lo esperado: la cuota (`CUOTA_CIUDADANO_BYTES`) solo se
   valida al cargar o sustituir un documento propio (`cargar_documento`, CU-05/CU-10) --
   nunca de forma continua. Un documento que deja de estar certificado no dispara nada
   por sí solo: no se borra nada, no se bloquea el acceso a lo que ya existe. El único
   efecto aparece la próxima vez que el ciudadano intente cargar o sustituir algo, que
   se rechaza con `CUOTA_AGOTADA` igual que a cualquiera ya al límite. No es un caso
   nuevo: un documento recibido por transferencia nunca respetó la cuota individual del
   ciudadano al recibirse (esa cuota siempre fue exclusiva de la carga propia), así que
   esto se suma a una situación que ya podía darse desde antes de esta sesión, no la
   inaugura. Documentado en docs/especificacion.md, "Documentos recibidos por
   transferencia (CU-16) y su firma" y "Parámetros y límites".

Probado de punta a punta extendiendo `scripts/probar_firma_transferencia.py`: además de
la revocación misma, verifica que aparece una notificación para el documento alterado
(y ninguna para el firmado), y que el ciudadano queda con `usado_bytes > cuota_bytes`
tras la revocación sin que nada se rompa -- confirmado intentando cargar un documento
nuevo justo después y viendo que se rechaza con 409 `CUOTA_AGOTADA`, exactamente como
se documentó.

Se verificó explícitamente que la imagen de Docker (`python:3.12-slim`, Linux, x86_64)
resuelve las dependencias de criptografía de pyHanko (`cryptography`, `lxml`) con
paquetes binarios ya compilados (manylinux), sin necesitar un compilador ni paquetes de
sistema adicionales en el `Dockerfile` -- se confirmó con una reconstrucción sin caché
de la imagen. Como Railway construye desde ese mismo `Dockerfile`, no debería haber
sorpresas ahí tampoco, aunque el despliegue real en Railway no se verificó en esta
tarea (ver "Qué se probó de verdad" de la última tarea reportada).

Probado de punta a punta con `scripts/probar_firma_digital.py` (contra `app-a` viva):
genera con pyHanko un PDF firmado, uno firmado y luego alterado, y uno sin firma;
carga los tres por CU-05, espera a que la bandeja de salida los procese, y confirma
`firma_valida=true` con firmante y fecha correctos para el firmado, `firma_valida=false`
para el alterado (sin que deje de existir), y `firma_valida` sin valor para el que no
tiene firma -- distinguido explícitamente comprobando que el trabajo de `outbox` sí
terminó `COMPLETADO` (no es que la validación no haya corrido, es que no encontró nada
que aplicar). Repite el caso firmado depositándolo por una entidad emisora (CU-13) y
confirma `certificado=true` junto con `firma_valida=true`.

`scripts/probar_firma_transferencia.py`, nuevo, prueba el enganche en CU-16 de punta a
punta: simula un operador de origen que envía dos documentos marcados `certificado:
true`, uno firmado de verdad y ese mismo alterado después de firmarse, contra `app-a`
con `mock-centralizador` (para que la recepción llegue de verdad hasta `ACTIVO`, no
como `scripts/probar_transferencia.py`, que usa la cédula segura del MinTIC real y por
eso nunca llega tan lejos). Confirma, contra la base de datos directamente (el
ciudadano recibido no tiene contraseña todavía): los dos documentos existen desde el
primer momento, el firmado termina `firma_valida=true` conservando `certificado=true`,
y el alterado termina `firma_valida=false` con el `certificado` retirado y su propia
auditoría de revocación -- sin dejar de existir. Se corrieron de nuevo como regresión
`probar_envio_transferencia.py`, `probar_regreso_antes_de_purga.py` y
`probar_reconciliacion.py` (los otros caminos que pasan por
`_recibir_transferencia`/`_reconciliar_transferencias`) y `probar_carpeta_completa.py`
/ `probar_colision_email.py`; los seis siguen sin fallos. Ninguno de los seis encontró
un bug nuevo -- confirmaron que el enganche no rompió nada de lo que ya pasaba por esa
misma función.

### Pendiente

**Entrega por correo cuando el destino no publica `transferAPIURL`** (spec, "Directorio
de operadores"): hoy CU-03 simplemente rechaza el envío en ese caso (`ValueError`, no
reintentable) en vez de repartir por correo electrónico.

**CU-04 completo (registro público de entidades) sigue sin implementar.** Solo existe
el mínimo que CU-13 necesita (`entidad_emisora` + `scripts/alta_entidad_emisora.py`, ver
arriba) — no hay registro por API ni consola. Revocar y reactivar sí existen (ver
arriba); lo que sigue faltando es dar de alta o consultar entidades sin acceso directo
a la base.

Notificaciones más allá del correo de registro, primer acceso, reenvío y depósito de un
documento por una entidad (ver arriba), consola de administración, y CU-06 sin
`descarga` masiva ni paquetes (RF27).

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
  main.py                      arranque, middleware de correlación, config de logging, ciclo de vida
  config.py  db.py  errors.py  configuración, sesión de BD, sobre de errores
  models.py                    las 9 tablas
  identidad/
    seguridad.py               hash de contraseñas (Argon2id) + regla de formato compartida
    token.py                   JWT de sesión (RS256)
    token_acceso.py            secretos opacos de alta entropia (SHA-256): primer acceso Y
                                clave de API de una entidad emisora (CU-13)
    dependencias.py            auth de sesión + resolver_usuario (cedula o email_carpeta)
    primer_acceso.py           POST /api/v1/primer-acceso y /primer-acceso/reenviar
    perfil.py                  GET/PATCH /api/v1/perfil (datos y cuota, CU-03 no incluido: ver interoperabilidad/)
  notificaciones/
    correo.py                  envío de correo simulado (sin proveedor real integrado) + registro en `notificacion`
    router.py                  GET /api/v1/notificaciones y POST .../leida (CU-17)
  documentos/
    router.py                  CU-05/06/07/08/10: carga, listado con filtros, consulta,
                                descarga, eliminación diferida y sustitución sin perder historia
    entidades.py              POST /api/v1/entidades/documentos: deposito certificado por
                                una entidad emisora autenticada (CU-13)
    firma.py                   CU-09: validacion de firma digital PAdES con pyHanko,
                                sin cadena de confianza (sin almacen ONAC)
    almacenamiento.py           unico cliente del bucket S3 (subir, bajar, borrar, enlaces firmados)
    tipos.py                    deteccion de content-type por contenido, no por extension
  interoperabilidad/
    govcarpeta.py              ÚNICO cliente del centralizador
    operadores.py              cliente de otros operadores (descarga, confirmAPI, envío)
    outbox.py                  bandeja de salida + mantenimiento periódico (directorio,
                                reconciliación, purga de transferencias y de documentos) +
                                emisión del token de primer acceso
    transferencias.py          router (/api, CU-16) + router_propio (/api/v1/perfil/traslado, CU-03)
  mock/registraduria.py        Registraduría simulada
alembic/versions/              migraciones
docs/especificacion.md         la especificación completa
scripts/probar_govcarpeta.py   prueba de humo contra la API real
scripts/limpiar_prueba.py      borra huella real de una cedula; pide confirmarla escribiendola de nuevo
scripts/probar_transferencia.py  simula un operador de origen enviando CU-16 a esta app
scripts/probar_envio_transferencia.py  CU-03+CU-16 entre dos instancias propias (ver mas abajo)
scripts/probar_reconciliacion.py  _reconciliar_transferencias en aislamiento, sin esperar horas
scripts/probar_regreso_antes_de_purga.py  el mismo ciudadano vuelve antes de su propia purga
scripts/probar_primer_acceso.py  consume el token de primer acceso y confirma el login
scripts/probar_reenvio_primer_acceso.py  token vencido -> reenvio -> token viejo sin servir
scripts/probar_carpeta_completa.py  CU-07/08/10/11/13/17 + perfil, y _purgar_documentos aislado
scripts/probar_colision_email.py  _recibir_transferencia en aislamiento: colision de email_carpeta entre dos cedulas
scripts/alta_entidad_emisora.py  alta, rotacion, revocacion y reactivacion de una entidad emisora (CU-13); no es una ruta publica
scripts/probar_firma_digital.py  CU-09: genera con pyHanko un PDF firmado/alterado/sin firma y valida los tres
scripts/probar_firma_transferencia.py  CU-09 en CU-16: documento certificado por el origen que llega firmado y alterado
scripts/mock_centralizador.py  centralizador falso en memoria, solo para esas pruebas
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

### CU-03 + CU-16 entre dos instancias propias

Mismo archivo, servicios distintos: `app-a` (emisor) y `app-b` (receptor), cada uno con
su **propia base de datos Postgres local y desechable** (`postgres-a`/`postgres-b`, no
tocan Supabase) y un **centralizador falso en memoria** (`mock-centralizador`,
`scripts/mock_centralizador.py`) que responde `validateCitizen`/`registerCitizen`/
`unregisterCitizen`/`getOperators` sin llegar nunca al MinTIC real. Sí comparten el
bucket S3 real (claves de objeto aleatorias, sin riesgo de choque). Como no hay TLS
entre contenedores, estas dos instancias corren con `TRANSFERENCIA_EXIGIR_HTTPS=false`
-- variable que en cualquier otro entorno debe quedar en `true`.

```bash
docker compose -f docker-compose.test.yml up --build -d postgres-a postgres-b mock-centralizador
docker compose -f docker-compose.test.yml up -d app-a app-b   # corren "alembic upgrade head" solos
docker compose -f docker-compose.test.yml run --rm prueba-envio             # CU-03 + CU-16 normal
docker compose -f docker-compose.test.yml run --rm prueba-reconciliacion   # sin esperar horas
docker compose -f docker-compose.test.yml run --rm prueba-colision-email   # colision de email_carpeta entre dos cedulas
docker compose -f docker-compose.test.yml run --rm prueba-regreso          # el mismo ciudadano vuelve
docker compose -f docker-compose.test.yml logs app-b | grep -A5 "correo simulado"   # token de primer acceso
docker compose -f docker-compose.test.yml run --rm prueba-primer-acceso \
  --base-url=http://app-b:8000 --token=<el-extraido-arriba> --usuario=<cedula>
docker compose -f docker-compose.test.yml run --rm prueba-reenvio-primer-acceso   # token vencido + reenvio
docker compose -f docker-compose.test.yml run --rm prueba-carpeta-completa   # CU-07/08/10/11/13/17 + perfil
docker compose -f docker-compose.test.yml run --rm prueba-firma-digital   # CU-09: firmado/alterado/sin firma
docker compose -f docker-compose.test.yml run --rm prueba-firma-transferencia   # CU-09 en CU-16
docker compose -f docker-compose.test.yml down -v             # -v: tambien borra postgres-a/b
```

`scripts/probar_envio_transferencia.py` registra un ciudadano de prueba en `app-a`
(cédula ficticia -- es seguro, nunca toca el MinTIC real), sube un documento, solicita
el traslado a `test-operador-b`, y verifica en las dos bases de datos (consulta directa
por SQL: este script es anterior a `GET /api/v1/perfil`, no se actualizó para usarlo) que
la transferencia quedó `CONFIRMADA`, el ciudadano llegó `ACTIVO` a `app-b` con su
documento, y `app-a` quedó `TRASLADADO`. Probado de punta a punta el 2026-09-22, varias
veces, de forma reproducible.

`scripts/probar_reconciliacion.py` inserta una `transferencia` `ENVIADA` con
`enviada_en` retrocedido en la base (no espera `TRANSFER_CONFIRM_TIMEOUT` de verdad) y
corre `_reconciliar_transferencias` una sola vez, cubriendo sus tres desenlaces
(confirmada, recuperada, ambigua). No depende de `app-a`/`app-b` como servidores vivos
-- solo de `postgres-a` y `mock-centralizador` -- para no competir con su propio outbox
por las mismas filas.

`scripts/probar_regreso_antes_de_purga.py` siembra directamente un ciudadano
`TRASLADADO` (con un documento y un objeto S3 real) y le envía una transferencia
entrante con la misma cédula y el mismo `email_carpeta`, contra `app-a` viva. Verifica
que el registro viejo (fila y objeto S3) se reemplaza y que el ciudadano llega a
`ACTIVO` en vez de quedarse colgado en `TRASLADADO`.

`scripts/probar_colision_email.py` siembra un ciudadano A ya `ACTIVO` con una
`email_carpeta` conocida y llama directamente a `_recibir_transferencia` (sin pasar por
`app-a` como servidor vivo, ni por `GovCarpeta` real: la colisión se detecta en el paso
2, antes de llegar a tocar el centralizador) con una transferencia entrante para una
cédula B distinta que trae ese mismo `citizenEmail`. Verifica que se rechace con un
`ValueError` que nombra explícitamente la cédula A (no un `IntegrityError` opaco de
Postgres), que no se cree ningún ciudadano para B, y que A quede intacto.

`scripts/probar_primer_acceso.py` consume el token de primer acceso del ciudadano que
`prueba-envio` acaba de dejar en `app-b` (extraído del log, no de la base: el token en
claro no se guarda en ningún lado), establece la contraseña, confirma que reusar el
mismo token ya falla, e inicia sesión con la contraseña nueva. Probado de punta a punta
el 2026-09-22, incluyendo por separado (con filas sembradas a mano) el camino de token
vencido y el de una transferencia sin `contactEmail`.

`scripts/probar_reenvio_primer_acceso.py` siembra un ciudadano pendiente de primer
acceso con un token YA vencido, confirma que no sirve, pide el reenvío, y confirma que
el token viejo _sigue_ sin servir después. Es la primera mitad de la prueba completa:
el token nuevo se extrae del log igual que arriba y se pasa a
`scripts/probar_primer_acceso.py` para terminar el ciclo (establecer la contraseña con
el token nuevo e iniciar sesión). Probado de punta a punta el 2026-09-22, más
verificación manual (llamadas directas dentro del contenedor) de las ramas no
cubiertas por el script: reenvío ignorado (ciudadano ya activo, cédula inexistente) y
reenvío limitado por exceder `PRIMER_ACCESO_REENVIO_MAXIMO` en la ventana.

`scripts/probar_carpeta_completa.py` registra su propio ciudadano de prueba en `app-a`
y cubre, contra la app viva: los filtros de CU-07, la sustitución de CU-10 (verifica que
el documento viejo sigue consultable como `REEMPLAZADO`, fuera del listado y de la
cuota), el borrado diferido de CU-08 (con sus rechazos: documento certificado y
documento ya reemplazado), el rechazo de CU-11 al pedir autenticación sobre un
documento `REEMPLAZADO` (409) o `ELIMINADO` (404), el depósito de CU-13 por una entidad
emisora sembrada directamente en la base (documento certificado, `entidad_emisora`
tomado de la credencial y no de lo declarado, sin impacto en la cuota, sustituyendo al
último documento temporal de la carpeta, y sin poder eliminarse después), la revocación
de esa entidad (401 con la misma clave de siempre) y su reactivación (vuelve a
funcionar, sin rotar), CU-17 (la notificación de registro y la del depósito de la
entidad aparecen en la bandeja, y se puede marcar una como leída) y `GET`/`PATCH
/api/v1/perfil`. Al final prueba `_purgar_documentos` en aislamiento, igual patrón que
`probar_reconciliacion.py`. Probado de punta a punta el 2026-09-22.

Las primeras seis, en conjunto, encontraron y corrigieron los bugs reales descritos
arriba en "Estado actual". `probar_carpeta_completa.py` no encontró un bug nuevo por sí
sola -- confirmó que CU-07/08/10/11/13/17 y perfil funcionan como se diseñaron; la
corrección de `autorizacion.documento_id` (`ON DELETE CASCADE`, ver arriba) se hizo por
revisión de código, anticipando el mismo tipo de bug ya visto con
`transferencia.ciudadano_id`, no porque la prueba la haya expuesto (CU-18 sigue sin
implementar, así que hoy no hay ninguna fila `autorizacion` que pudiera activarlo).
`probar_colision_email.py` tampoco encontró un bug nuevo: confirmó la política de
colisión de `email_carpeta` entre dos cédulas distintas, decidida y documentada como
parte de esta misma tarea (docs/especificacion.md, "Interoperabilidad entre
operadores"), no un comportamiento que ya existiera sin probar.

`scripts/probar_firma_digital.py` genera con pyHanko un certificado autofirmado ad-hoc
(nunca se pretende que sea confiable -- solo sirve para ejercitar la validación
criptográfica) y tres PDF de prueba: uno firmado, ese mismo alterado después de
firmarse (cambia un byte del contenido visible), y uno sin firma. Carga los tres por
CU-05 contra `app-a` viva, espera a que la fila de `outbox` de cada uno termine
`COMPLETADO`, y verifica: el firmado da `firma_valida=true` con el firmante y la fecha
correctos; el alterado da `firma_valida=false` sin dejar de existir (una firma inválida
no rechaza el documento); el que no tiene firma da `firma_valida` sin valor -- se
confirma consultando la fila de `outbox` directamente, porque para ese caso
`firma_valida` nunca cambia y por sí solo no probaría que la validación corrió. Repite
el PDF firmado depositándolo por una entidad emisora (CU-13) y confirma
`certificado=true` junto con `firma_valida=true`. Tampoco encontró un bug nuevo:
confirmó que `app.documentos.firma` funciona como se diseñó, incluida la ausencia
deliberada de validación de cadena de confianza. Probado de punta a punta el
2026-09-22, junto con una reconstrucción sin caché de la imagen de `app-a` para
confirmar que pyHanko y sus dependencias de criptografía (`cryptography`, `lxml`)
instalan con paquetes binarios ya compilados en Linux, sin tocar el `Dockerfile`.

`scripts/probar_firma_transferencia.py` prueba el enganche de CU-09 en CU-16. Sirve dos
PDF (firmado, y ese mismo alterado) desde su propio servidor HTTP local -- mismo patrón
que `confirmAPI` en `scripts/probar_transferencia.py` -- y envía un
`POST /api/transferCitizen` a `app-a` con los dos marcados `certificado: true` en
`documentsMetadata` (la palabra del operador de origen, no autenticada). A diferencia
de `probar_transferencia.py` (que usa la cédula ya afiliada a otro operador real y por
eso la recepción se descarta antes de terminar), este corre contra `mock-centralizador`
para que `validateCitizen` diga "disponible" y la recepción llegue de verdad hasta
`ACTIVO`. Verifica contra la base de datos (el ciudadano recibido no tiene contraseña
todavía: no vale la pena pasar por primer acceso solo para esta lectura) que los dos
documentos existen desde el primer momento, que el firmado conserva
`certificado=true` con `firma_valida=true`, y que el alterado pierde el `certificado`
(`firma_valida=false`, con su propia auditoría de revocación) sin dejar de existir.
Probado de punta a punta el 2026-09-22, junto con `probar_envio_transferencia.py`,
`probar_regreso_antes_de_purga.py` y `probar_reconciliacion.py` como regresión (los
otros caminos que ya pasaban por `_recibir_transferencia`): los cuatro siguen sin
fallos.

## Convenciones

- Nombres de dominio en español (`ciudadano`, `documento`, `outbox`), código en inglés
  donde sea idiomático de la librería.
- Errores de la API propia siempre con el sobre `{"error": {...}}` de `app/errors.py`.
  Los endpoints de transferencia entre operadores NO usan ese formato: responden con lo
  que define el acuerdo del ecosistema, para no romper a los demás operadores.
- Un módulo por componente lógico bajo `app/`. Las fronteras entre módulos se respetan
  aunque hoy compartan proceso: la arquitectura objetivo separa `interoperabilidad`.
- Cuando termines una parte, actualiza la sección **Estado actual** de este archivo.
- **Los docstrings de las rutas son documentación pública.** FastAPI los publica
  tal cual en `/docs`, que es parte de la entrega y la ve el profesor. Deben
  decir qué hace la operación, qué recibe, qué devuelve y qué errores da. Nunca
  nombres de módulos internos, rutas de archivos del repositorio, referencias a
  este `CLAUDE.md` ni justificaciones de diseño: eso va en comentarios dentro de
  la función. Al crear o modificar una ruta, revisa que su docstring siga
  cumpliendo esto.

## Cómo reportar al terminar una tarea

El resumen final es lo único que el usuario ve del trabajo. Se escribe **en
español**, en lenguaje llano, y con esta estructura fija:

**Antes de reportar, revisa tu propio diff buscando borrados no pedidos.** Que
la aplicación arranque solo prueba que no falta nada de lo que corre al inicio;
una función auxiliar o una validación dentro de una rama poco frecuente puede
desaparecer sin que nada falle hasta mucho después. Cada línea eliminada debe
corresponder a algo que la tarea pedía cambiar.

**1. Qué quedó hecho.** Una línea por cada cosa que se pidió, en el mismo orden
en que se pidió, marcada como hecha, parcial o no hecha. Ninguna de las cosas
pedidas puede faltar en esta lista, ni siquiera las que parecen menores: si no
se hizo, se dice que no se hizo.

**2. Qué no se hizo y por qué.** Incluye lo que quedó fuera de alcance a
propósito y lo que se intentó y no salió.

**3. Decisiones tomadas que no estaban en la instrucción.** Cualquier criterio
que se eligió sin que nadie lo pidiera, con la razón en una línea.

**4. Qué se probó de verdad.** Distingue con claridad tres cosas distintas: lo
que se ejecutó y funcionó, lo que se verificó leyendo el código sin ejecutarlo,
y lo que no se probó. Nunca presentar lo segundo o lo tercero como lo primero.
Si una prueba falló, se dice, aunque después se haya corregido.

**5. Qué se tocó de lo real.** Base de datos de producción, bucket real,
directorio del MinTIC, archivos dejados atrás. Si no se tocó nada real, decirlo
explícitamente.

**Antes de reportar, revisa tu propio diff buscando borrados no pedidos.** Que
la aplicación arranque solo prueba que no falta nada de lo que corre al inicio;
una función auxiliar o una validación dentro de una rama poco frecuente puede
desaparecer sin que nada falle hasta mucho después. Cada línea eliminada debe

**6. Comandos que debe ejecutar el usuario.** Siempre explícito, aunque sean
cero: decir "no hay nada que ejecutar" en vez de omitir la sección. Si una
migración ya se aplicó, decirlo, para que no la corra dos veces.

**7. Nombre de commit propuesto**, con cuerpo cuando el cambio lo amerite.

No escribas "listo" sobre algo que no ejecutaste. No describas como implementado
lo que quedó a medias. Si durante la tarea descubriste que algo que el proyecto
daba por hecho no era cierto, dilo aunque nadie lo haya preguntado.
