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
centro de notificaciones, CU-17) · `app/portal/` (AD-11: todas las pantallas del
ciudadano -- sesión, registro, carpeta, notificaciones, perfil, segundo factor,
traslado, primer acceso) · `app/admin/` (AD-12: consola de administración de solo
lectura, RF32-RF37/CU-22) · `scripts/limpiar_prueba.py` ·
`scripts/probar_transferencia.py` · `scripts/probar_envio_transferencia.py` ·
`scripts/mock_centralizador.py` · `scripts/probar_carpeta_completa.py` ·
`scripts/alta_entidad_emisora.py` · `scripts/probar_colision_email.py` ·
`scripts/probar_firma_digital.py` · `scripts/probar_firma_transferencia.py` ·
`scripts/probar_portal.py` · `scripts/probar_portal_segunda_pasada.py` ·
`scripts/probar_portal_primer_acceso.py`.

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
`transferencia.reconciliacion_ambigua`). Los tres pasaron. **Este diseño de tres
desenlaces quedó reemplazado el 2026-09-23** por uno de solo dos preguntas ("¿sigue
siendo nuestro?"), que elimina el caso ambiguo -- ver más abajo, en el bloque fechado
2026-09-23.

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

**Portal del ciudadano (AD-11), primera pasada, implementado y probado de punta a
punta el 2026-09-22.** Pantallas HTML servidas por la misma aplicación FastAPI (Jinja2
+ HTMX, sin framework de JavaScript, sin paso de build ni segunda unidad desplegable):
iniciar sesión, registro, y la carpeta (listar con filtros, subir, ver el detalle de un
documento con su estado de autenticación ante GovCarpeta, descargar, eliminar). Las
demás pantallas (perfil, notificaciones, traslado a otro operador) quedan para la
segunda pasada.

La lógica de negocio de CU-01/CU-02 se extrajo de `app/identidad/registro.py` y
`app/identidad/sesion.py` a `app/identidad/servicios.py`; la de CU-05/06/07/08/11 se
extrajo de `app/documentos/router.py` a `app/documentos/servicios.py` (incluido
`resolver_sustitucion`, que `app/documentos/entidades.py` importa ahora de ahí). Las
rutas JSON quedaron como envoltorios delgados sobre esas mismas funciones -- se
verificó que el esquema OpenAPI sigue reportando las mismas 19 rutas después de cada
extracción, y que ningún código ni mensaje de error cambió. El portal (`app/portal/`)
llama a esas mismas funciones directamente, nunca por HTTP contra su propia API: ver
AD-11 (docs/especificacion.md) para la justificación completa.

La sesión del portal viaja en una cookie (`app.portal.auth`, nombre
`colcarpeta_sesion`) HttpOnly + SameSite=Strict + Secure (controlada por
`PORTAL_COOKIE_SECURE`, `true` por defecto -- **nunca en `false` en `.env` real ni en
Railway**, mismo patrón que `TRANSFERENCIA_EXIGIR_HTTPS`), que transporta el mismo JWT
que ya emite `app.identidad.token` para la API: cerrar sesión en el portal borra la
cookie pero no invalida el token si se usó también contra la API directamente (mismo
comportamiento ya documentado para `DELETE /api/v1/sesion`). El portal no aparece en
el esquema OpenAPI (`include_in_schema=False`): no es parte del contrato de la API.

**El portal vive en la raíz del dominio, no bajo `/portal/...`.** Vivió ahí en un
primer momento y se movió a la raíz el mismo día (2026-09-22), porque la raíz es lo
primero que ve cualquiera que entre al dominio y dejarla en blanco (404) no tenía
sentido. Rutas actuales: `/` (lleva a `/carpeta` si hay sesión, a `/sesion` si no),
`/sesion`, `/registro`, `/carpeta`, `/carpeta/documentos`, `/documentos/{id}`,
`/documentos/{id}/descarga`, `/documentos/{id}/eliminar`,
`/documentos/{id}/autenticacion`, `/salir`. Los estáticos (HTMX, la hoja de estilos)
están en `/static/...`. Nada de esto choca con `/api/...` (la API sigue exactamente
donde estaba, es el contrato con los otros operadores y con las entidades emisoras),
ni con `/health`, `/mock/...`, `/docs`, `/redoc` u `/openapi.json` -- se verificó
generando el esquema OpenAPI (sigue en 19 rutas) y arrancando la aplicación sin
errores de ruta duplicada. Cada ruta vieja bajo `/portal/...` (`app/portal/router.py`,
`router_legado`) redirige de forma permanente (308, preserva método y cuerpo -- a
diferencia de 301/302/303, que en la práctica convierten un `POST` en `GET`) hacia su
equivalente nueva, con la cadena de consulta intacta, por si alguien guardó un
enlace; incluye `/portal` (raíz vieja) y `/portal/static/{ruta}` (estáticos viejos).

**`SameSite=Strict` es toda la protección contra falsificación de peticiones entre
sitios (CSRF) de esta pasada -- no se agregó un token CSRF de doble envío aparte.**
Con `Strict`, el navegador nunca adjunta la cookie en una petición que se origina en
otro sitio, ni siquiera en una navegación de nivel superior (un enlace externo hacia el
portal): cualquier formulario que un sitio atacante intente enviar hacia
`/carpeta/documentos`, `/documentos/{id}/eliminar`, etc., llega sin la
cookie, `ciudadano_actual_portal` no resuelve a nadie, y la operación termina en un
redirect a iniciar sesión en vez de ejecutarse -- no hay ninguna petición de este
portal que dependa de una cookie `Lax` o sin `SameSite` para funcionar. Es la defensa
que recomienda OWASP para este patrón (sesión en cookie, sin necesidad de que el
ciudadano llegue autenticado desde un enlace de otro sitio), y basta porque las únicas
mutaciones del portal son `POST` que exigen esa misma cookie -- no hay ninguna
operación de escritura detrás de un `GET`. La única limitación conocida es de
navegadores muy antiguos que no implementan `SameSite` (tratan la cookie como si no
lo tuviera, el mismo riesgo que ya existía antes de esta cookie), no un hueco en la
implementación. Verificado leyendo el encabezado `Set-Cookie` real que emite
`fijar_cookie_sesion` (trae `HttpOnly; SameSite=strict; Secure`); el cumplimiento de
`SameSite` en sí lo hace el navegador, no el servidor, así que no hay forma de
ejercitarlo con `httpx` (que no implementa esa política) dentro de las pruebas de
Docker de esta tarea -- queda verificado por inspección del encabezado y por lo que
especifica el estándar, no por una prueba de navegador real.

`PORTAL_COOKIE_SECURE` es seguro por defecto (`true`) cuando la variable no está
definida -- confirmado instanciando `Config()` sin la variable en el entorno ni en
`.env`. El `.env` real y `.env.example` no la fijan (ver arriba, no hace falta:
el default ya es el correcto), así que en Railway la cookie sale con `Secure` sin
que nadie tenga que declarar nada; el único lugar del repositorio donde se pone en
`false` es el entorno de `app-a` en `docker-compose.test.yml`, para las pruebas
locales por HTTP simple entre contenedores.

Cómo se muestra un documento (decisión de presentación, no de backend): "Certificado"
si `documento.certificado` es verdadero, "Temporal" si no, con la nota en letra
pequeña "Información proporcionada por ti" bajo lo temporal -- sin alarmas, el
ciudadano no está haciendo nada indebido al subir su propio documento. El estado de la
firma respeta sus tres valores reales sin colapsarlos: "Sin firma digital"
(`firma_valida` es NULL), "Firma digital válida" (`true`), "Firma digital inválida"
(`false`) -- las dos macros que deciden esta presentación viven en
`app/portal/templates/_macros.html` para no repetir la decisión entre la lista y el
detalle.

La carga de un documento responde de inmediato aunque la validación de firma siga
corriendo por detrás: eso ya era cierto a nivel de API (la validación la hace la
bandeja de salida en segundo plano, AD-05) y el portal simplemente no espera por
ella -- no se agregó ningún mecanismo de sondeo o actualización en vivo en esta
pasada; para ver el resultado de una firma que se estaba validando al momento de
subir, el ciudadano recarga la página del detalle.

HTMX (2.0.4, vendorizado en `app/portal/static/htmx.min.js`, sin CDN) se usa para que
eliminar un documento desde la lista de la carpeta no recargue la página completa
(`hx-post` sobre un formulario real, que sigue funcionando sin JavaScript por
degradación progresiva -- eliminar desde el detalle de un documento, en cambio, usa un
formulario corriente sin HTMX porque esa página deja de tener sentido una vez que el
documento se elimina). El resto de la navegación (filtros, paginación, subir, entrar)
son formularios y enlaces corrientes: totalmente operables con teclado y sin
JavaScript, cumpliendo el requisito de usabilidad del caso de estudio. La hoja de
estilos (`app/portal/static/estilos.css`) es mobile-first, sin ningún framework de CSS,
con foco visible (`:focus-visible`) y un enlace para saltar al contenido.

Los formularios muestran los mensajes de `ErrorDeNegocio` tal cual (ya están escritos
en lenguaje llano en toda la API, p. ej. "Usuario o contrasena incorrectos.") en vez de
inventar una segunda capa de traducción; los errores de formato de Pydantic (cédula,
correo, contraseña) se recogen aparte y se unen en una frase legible
(`_mensaje_validacion` en `app/portal/router.py`). El login re-muestra el formulario
con un segundo campo cuando el ciudadano tiene el segundo factor habilitado
(`SEGUNDO_FACTOR_REQUERIDO`), sin necesidad de una pantalla aparte.

No se expuso `sustituye_a` (CU-10) en el formulario de carga de esta primera pasada:
sigue disponible por la API: se dejó fuera para no ampliar el alcance de "los
cimientos y la carpeta" con una interacción de selección de documento que no se pidió
explícitamente.

Probado de punta a punta con `scripts/probar_portal.py`, contra `app-a` viva dentro de
`docker-compose.test.yml` (con `PORTAL_COOKIE_SECURE=false`, nueva variable de entorno
de ese servicio -- ver "Trampas" más abajo): registro por el formulario HTML, login con
contraseña incorrecta primero (mensaje en lenguaje claro) y luego correcta, la cookie
de sesión se fija y el cliente HTTP la conserva entre peticiones igual que un
navegador, la carpeta vacía muestra el mensaje correcto, subir un documento lo deja
visible de inmediato con la etiqueta "Temporal" y su nota de procedencia, el detalle
muestra "Sin firma digital" (el PDF de prueba no está firmado) y la sección de
autenticación ante GovCarpeta, la descarga devuelve exactamente los mismos bytes que
se subieron, eliminar lo saca del listado con su mensaje de éxito, y cerrar sesión hace
que `/carpeta` vuelva a exigir inicio de sesión. Aparte, se verificaron
directamente (renderizando `_macros.html` con Jinja2 en aislamiento, sin pasar por
Docker) las seis combinaciones de `certificado`/`firma_valida` sobre las macros de
presentación, para cubrir también los casos de firma válida e inválida que el
documento de prueba (sin firma real) no ejercita por sí solo.

**Trampa nueva, encontrada al probar el portal (2026-09-22), no documentada en ningún
lado hasta ahora:** una cookie `Secure` nunca se envía sobre una conexión HTTP simple
-- ni un navegador real ni el cliente de pruebas (`httpx`, que respeta esa regla igual
que el `http.cookiejar` estándar de Python) la reenvía en la siguiente petición. La
primera corrida de `probar_portal.py` contra `app-a` (que dentro de
`docker-compose.test.yml` habla HTTP simple entre contenedores, sin TLS) parecía que el
login fallaba en silencio -- sin ningún error de credenciales, la página simplemente
volvía a mostrar el formulario de inicio de sesión vacío -- hasta confirmar que la
cookie sí se fijaba en la respuesta pero nunca volvía en la petición siguiente.
Corregido agregando `PORTAL_COOKIE_SECURE=false` al entorno de `app-a` en
`docker-compose.test.yml`, mismo patrón ya usado para `TRANSFERENCIA_EXIGIR_HTTPS`.
**La misma trampa volvió a aparecer con `app-b` al probar la segunda pasada** (ver
abajo): faltaba la misma variable en el entorno de `app-b`, que hasta ahora nunca había
servido pantallas del portal en las pruebas de Docker. Corregida de la misma forma.

**Portal del ciudadano, segunda pasada, implementada y probada de punta a punta el
2026-09-23.** Las seis pantallas que quedaban pendientes: notificaciones (CU-17, con
el contador de no leídas visible en la navegación de cualquier pantalla), perfil (datos
y cuota), segundo factor (habilitar/confirmar/deshabilitar TOTP), traslado a otro
operador (CU-03), primer acceso por token y su reenvío, y sustituir un documento
temporal (CU-10, que había quedado sin pantalla en la primera pasada). Mismo patrón que
la primera pasada en todo: capa de servicios compartida entre la API JSON y el portal,
ninguna llamada HTTP del portal a sí mismo, plantillas Jinja2 + HTMX.

Cinco módulos nuevos de servicios, cada uno extraído de la ruta JSON que ya existía
(el mismo patrón de extracción de la primera pasada, ahora aplicado a lo que quedaba):
`app/notificaciones/servicios.py` (de `app/notificaciones/router.py`),
`app/identidad/perfil_servicios.py` (de `app/identidad/perfil.py` y
`app/identidad/perfil_totp.py` juntos, porque el portal los presenta como una sola
sección "perfil y seguridad"), `app/identidad/primer_acceso_servicios.py` (de
`app/identidad/primer_acceso.py`, incluido el modelo Pydantic `SolicitudPrimerAcceso`
con su validador de formato de contraseña, movido junto con la función para que el
portal comparta la misma regla sin repetirla) y
`app/interoperabilidad/traslado_servicios.py` (de la parte `router_propio` de
`app/interoperabilidad/transferencias.py` -- CU-16, en el mismo archivo, no se tocó:
sigue con su propio formato de intercambio, ajeno al portal). Las cuatro rutas JSON
quedaron como envoltorios delgados, igual que en la primera pasada; se verificó de
nuevo que el esquema OpenAPI sigue en las mismas 19 rutas.

Cinco archivos nuevos de rutas del portal, cada uno con su propio `APIRouter` incluido
por separado en `app/main.py` (en vez de seguir agregando todo a
`app/portal/router.py`, que ya llevaba las pantallas de la primera pasada más las de
sustituir documento y el estado en vivo de un documento, que sí se quedaron ahí por ser
extensiones directas de la carpeta): `app/portal/router_notificaciones.py`,
`app/portal/router_perfil.py`, `app/portal/router_traslado.py` y
`app/portal/router_primer_acceso.py`. Ninguno de los cuatro tiene rutas viejas que
redirigir (`router_legado`): son pantallas nuevas, nunca vivieron en otro lado.

**Estado legible de lo que corre por detrás, con HTMX, sin recargar a ciegas:**
- El detalle de un documento (`_documento_estado.html`) sondea
  `GET /documentos/{id}/estado` cada 5 s mientras la firma todavía se está validando
  (`app.documentos.servicios.firma_en_validacion`, nueva: consulta si existe una fila
  `outbox` de `validarFirma` para ese documento todavía `PENDIENTE`/`EN_PROCESO` --
  distingue "se está validando" de "no hay nada que validar", que `firma_valida=NULL`
  por sí solo no puede distinguir) o mientras `estado_autenticacion` sigue `PENDIENTE`.
  Dejar de sondear es automático: el fragmento devuelto deja de traer el atributo
  `hx-trigger` en cuanto ambas cosas se resuelven.
- El traslado (`_traslado_estado.html`) sondea `GET /perfil/traslado/estado` cada 5 s
  mientras el ciudadano está `EN_TRANSFERENCIA` o la `Transferencia` sigue `ENVIADA` --
  las dos condiciones por separado porque la fila `Transferencia` la crea recién el
  manejador de outbox, un rato después de que la ruta ya marcó al ciudadano
  `EN_TRANSFERENCIA`; sin contar también el estado del ciudadano, la pantalla mostraría
  "tu carpeta no está activa" en esa ventana en vez de "tu traslado está en proceso" --
  encontrado y corregido durante esta misma tarea, antes de la prueba en Docker, al
  revisar el código, no por una prueba que lo haya expuesto.
- El contador de notificaciones en la navegación (`_notificaciones_contador.html`,
  dentro de `base.html`, visible en cualquier pantalla autenticada) sondea
  `GET /notificaciones/contador` cada 30 s, siempre, sin condición de parada: no hay un
  estado "resuelto" para una bandeja de notificaciones.

**El traslado (CU-03) es la operación más grave del sistema y así se trata en la
pantalla.** `GET /perfil/traslado` nunca deja escribir el operador destino a mano: es
un `<select>` sobre `traslado_servicios.listar_operadores_transferibles()` (los del
directorio con `transfer_api_url`, filtrados por `https://` si
`TRANSFERENCIA_EXIGIR_HTTPS` lo exige). El formulario exige además una casilla de
confirmación explícita (`confirmar`), verificada tanto por el atributo `required` del
navegador como -- la protección real -- del lado del servidor: sin la casilla marcada
(o con una petición cruda que la omita), `POST /perfil/traslado` la rechaza con el mismo
mensaje en lenguaje claro, sin siquiera mirar si el operador elegido existe.

**Sustituir un documento (CU-10) reutiliza `documentos_servicios.cargar_documento` tal
cual**, con `sustituye_a` fijado al documento que se está reemplazando: no fue necesario
tocar la capa de servicios de documentos para esto, solo agregar las dos rutas del
portal (`GET`/`POST /documentos/{id}/sustituir`, en `app/portal/router.py`) y su
plantilla. La ruta rechaza (con un redirect a un mensaje en lenguaje claro) sustituir un
documento certificado o que ya no esté `ACTIVO`, aunque esa misma validación ya la hace
`resolver_sustitucion` del lado del servicio -- es una comprobación redundante a
propósito, para no mostrarle al ciudadano un formulario que de todas formas fallaría al
enviarlo.

Probado de punta a punta con dos scripts nuevos, contra `app-a`/`app-b`/
`mock-centralizador` reales en Docker:

- `scripts/probar_portal_segunda_pasada.py`: registro, login, subir un documento,
  sustituirlo (CU-10, verifica que el original queda consultable como reemplazado),
  notificaciones (listar, marcar como leída, el contador), perfil (consultar y
  actualizar), segundo factor (habilitar con el código simulado, confirmar, ver el
  estado en `/perfil` y en `/perfil/totp`, deshabilitar), y el traslado completo:
  rechazo sin la casilla marcada, solicitud real a `test-operador-b`, espera activa
  hasta que se confirma, y verificación de que el login después responde con el
  mensaje de "carpeta ya trasladada". Encontró y corrigió, antes de considerarse
  terminado, un bug real de _timing_ propio de esta tarea (el de `en_curso` descrito
  arriba) -- no estaba en el código de la primera pasada, lo introdujo esta misma
  pantalla.
- `scripts/probar_portal_primer_acceso.py`: primer acceso por el portal
  (`GET`/`POST /primer-acceso`) con el token REAL que la corrida anterior dejó en el
  log de `app-b` (mismo patrón de extracción manual que
  `scripts/probar_primer_acceso.py`, que prueba lo mismo contra la API JSON): abre el
  enlace con el token en la URL (precargado en el campo), establece la contraseña,
  confirma que reusar el mismo token ya falla con el mensaje en lenguaje claro, e
  inicia sesión por el portal con la contraseña nueva. Encontró la misma trampa de la
  cookie `Secure` que ya se había corregido en `app-a`, esta vez en `app-b` (ver
  arriba) -- ningún otro bug.

Como regresión sobre las rutas JSON que comparten la capa de servicios recién
extraída, se corrieron de nuevo `scripts/probar_carpeta_completa.py`,
`scripts/probar_envio_transferencia.py`, `scripts/probar_primer_acceso.py`,
`scripts/probar_reconciliacion.py` y `scripts/probar_regreso_antes_de_purga.py`: los
cinco siguen sin fallos. También se verificaron 43 combinaciones de contexto sobre las
plantillas nuevas y modificadas con Jinja2 en aislamiento (sin Docker), para cubrir
ramas que las pruebas de Docker no ejercitan todas (p. ej. firma inválida, traslado ya
confirmado, traslado fallido, TOTP habilitado al cargar la pantalla).

**Reconciliación de transferencias simplificada el 2026-09-23
(`app.interoperabilidad.outbox._reconciliar_transferencias`).** La pregunta que
responde es una sola: ¿el ciudadano sigue siendo nuestro? `204` (disponible) o `200`
nombrando a ColCarpeta mismo → `FALLIDA`, se recupera con `registerCitizen` -- son el
mismo desenlace, porque en ambos casos el ciudadano nunca dejó de ser nuestro (el
segundo caso es nuevo: significa que `unregisterCitizen` del envío original no surtió
efecto, algo que antes de esto no se distinguía de "no afiliado a nadie"). `200`
nombrando a cualquier otro operador, sea o no el destino exacto elegido → `CONFIRMADA`,
igual que antes. **Desaparece el tercer desenlace ambiguo** que antes dejaba la
transferencia colgada en `ENVIADA` para siempre cuando el centralizador nombraba a un
operador distinto del destino: la pregunta nunca fue "¿llegó a donde lo mandamos?", es
"¿sigue siendo nuestro?", y esa pregunta el centralizador siempre la responde sin
ambigüedad. `transferencia.reconciliacion_ambigua` deja de generarse.
`scripts/probar_reconciliacion.py` pasó de tres escenarios a cuatro (A: confirmada por
el destino exacto; B: disponible, recuperada; C, antes "ambigua", ahora confirmada
igual que A porque quedó afiliado a un tercero; D, nuevo: afiliado a ColCarpeta mismo,
recuperada igual que B) -- los cuatro probados de punta a punta contra
`mock-centralizador` el 2026-09-23, sin fallos.

**La pantalla de traslado ahora dice cuánto puede tardar y qué pasa si falla.**
`horas_maximo` (`TRANSFER_CONFIRM_TIMEOUT / 3600`, calculado en
`app.portal.router_traslado._contexto_estado`, nunca escrito a mano en una plantilla)
aparece tanto en la advertencia antes de confirmar el traslado como en la pantalla de
estado mientras está en curso, junto con "si no se completa, tu carpeta vuelve a estar
activa en ColCarpeta sola, sin que tengas que hacer nada" -- antes la pantalla de "en
proceso" no daba ningún horizonte de tiempo ni decía qué pasaba si fallaba.

**El sondeo del contador de notificaciones subió de 30 segundos a 10 minutos
(`NOTIFICACIONES_CONTADOR_INTERVALO_SEGUNDOS`, nueva variable de configuración, `600`
por defecto).** Vive en `app/config.py`, no escrito en ninguna plantilla: se inyecta
una sola vez como global de Jinja (`app.portal.router`, `templates.env.globals`) que
`base.html` y `_notificaciones_contador.html` leen por igual, así que ajustarlo no
exige tocar HTML. La insignia igual se repinta con datos frescos en cada cambio de
pantalla (`hx-trigger="load, ..."`); el sondeo periódico solo cubre a alguien que se
queda quieto en la misma pantalla mucho tiempo, donde un intervalo más largo no se
nota.

**Revisión completa de tildes en el texto visible del portal, el 2026-09-23.** Títulos
de página, etiquetas, botones, mensajes de éxito/error y los asuntos/cuerpos de los
correos simulados (que también quedan en la bandeja de notificaciones del ciudadano,
CU-17) -- en las plantillas Jinja2, en los mensajes de `ErrorDeNegocio` de las capas de
servicios que el portal comparte con la API JSON (`identidad/servicios.py`,
`perfil_servicios.py`, `primer_acceso_servicios.py`, `documentos/servicios.py`,
`notificaciones/servicios.py`, `traslado_servicios.py`), y en
`identidad/seguridad.py` (el mensaje de formato de contraseña, mostrado en el portal
vía la validación de Pydantic). **Deliberadamente fuera de esta revisión:**
`app/documentos/entidades.py` (API de entidades emisoras, nunca renderizada por el
portal) y cualquier identificador de código (nombres de variable, campos, rutas) --
solo texto que un ciudadano llega a leer. Como varios de esos mensajes de
`ErrorDeNegocio` también los devuelve la API JSON tal cual, sus respuestas de error
ahora también salen con tildes correctas; no cambió ningún código ni estructura, solo
el texto de `mensaje`. Todos los scripts de prueba que comparaban texto exacto contra
esos mensajes (`probar_portal.py`, `probar_portal_segunda_pasada.py`,
`probar_portal_primer_acceso.py`) se actualizaron para seguir pasando -- sin eso,
habrían quedado rotos por el cambio de texto, no por una regresión real.

Las cuatro correcciones de esta tarea se probaron de punta a punta en Docker: los
cuatro escenarios de reconciliación (arriba), y las tres pruebas del portal
(`probar_portal.py`, `probar_portal_segunda_pasada.py` con el traslado real hasta
`CONFIRMADA` mostrando el nuevo texto de horas/recuperación automática,
`probar_portal_primer_acceso.py` con un token real) junto con la regresión JSON
completa (`probar_carpeta_completa.py`, `probar_envio_transferencia.py`,
`probar_primer_acceso.py`, `probar_regreso_antes_de_purga.py`) -- todas sin fallos tras
los ajustes. Al confirmar el traslado real en Docker se verificó directamente en los
logs de `app-b` que el correo simulado de primer acceso también sale con tildes
("Tu carpeta se trasladó a ColCarpeta...").

**Dirección visual del portal aplicada el 2026-09-23, contra `docs/diseno.md`
(documento nuevo, no escrito en esta tarea -- ya existía como especificación de
diseño).** Cambio de piel puro: ninguna ruta, contrato de API, ni estructura de
plantilla se tocó más allá de lo que el documento pedía explícitamente. Se modificaron
`app/portal/static/estilos.css` (paleta, tipografía, tamaños de campo/botón,
`.etiqueta`), `app/portal/templates/base.html` (favicon/manifest/theme-color, fuentes
de Google, ícono junto a la marca en el encabezado, nuevo bloque `main_extra_class`) y
`app/portal/templates/sesion.html`/`registro.html` (nueva tarjeta partida
`.entrada`/`.entrada__forma`/`.entrada__panel`, con el panel invertido en registro).
Los archivos de marca (`favicon.ico`, `icono.svg`, `icono-180/192/512.png`,
`site.webmanifest`) ya estaban en `app/portal/static/` antes de esta tarea; `marca.svg`
queda sin usar dentro del portal a propósito (el documento lo reserva para fuera del
navegador).

Dos puntos que el documento señalaba explícitamente para confirmar: `etiqueta--certificado`
pasó del verde (que compartía con `etiqueta--firma-valida`) a un ocre propio
(`--color-certificado-fondo`/`--color-certificado-texto`) -- el verde
(`--color-exito-fondo`/`--color-exito-texto`) queda ahora exclusivo de la firma
verificada y de los avisos de éxito genéricos. Y la variable `--color-suave`, que antes
hacía de fondo de página y de relleno sutil a la vez, se separó en dos: `body` pasa a
usar la nueva `--color-fondo-pagina`, y se revisaron uno por uno los cinco usos
restantes de `--color-suave` (`.boton--secundario:hover`, `.etiqueta--temporal`,
`.etiqueta--firma-ausente`, `.barra-cuota`, `.valor-destacado`) -- los cinco eran ya
relleno sutil legítimo y no cambiaron de valor, solo se les quitó el `border` que
`etiqueta--temporal`/`etiqueta--firma-ausente` tenían antes, por pedido del documento.
Ninguno quedó viéndose raro tras la separación.

Probado de punta a punta en Docker, reconstruyendo `app-a`/`app-b` con el nuevo CSS y
plantillas: `prueba-portal` (registro, login, subir, detalle, descargar, eliminar,
redirecciones `/portal/...`, salir), `prueba-portal-segunda-pasada` (notificaciones,
perfil, TOTP, sustituir CU-10, traslado CU-03 completo hasta `CONFIRMADA`) y
`prueba-portal-primer-acceso` (con un token real extraído del log de `app-b`) -- las
tres sin fallos, sin necesidad de ajustar ninguna aserción de texto o de clase. Como
regresión sobre la API JSON (que este cambio no debería tocar en absoluto, al ser
puramente de plantillas/CSS del portal): `prueba-carpeta-completa`,
`prueba-envio-transferencia` (con `prueba-reconciliacion`, sus cuatro escenarios, y
`prueba-regreso-antes-de-purga` encima) y `prueba-primer-acceso` (con un segundo token
real) -- las cinco sin fallos. Se verificó además, contra el contenedor vivo por
`curl`, que `/static/favicon.ico` responde 200 con `image/vnd.microsoft.icon` y que el
`<head>` de `/sesion` trae los `<link rel="icon">`/`<link rel="apple-touch-icon">`
esperados, y que el encabezado renderiza el `<img>` de `icono.svg` junto al nombre.

**Tres correcciones más de `docs/diseno.md` aplicadas el 2026-09-23** (el documento se
amplió con secciones 5-final, 6 y 7 nuevas después de la pasada anterior): campos
obligatorios que ya no se pintan de rojo al cargar la página, navegación del
encabezado con semántica propia, y la carpeta separada en dos páginas.

- **`input:invalid` → `input:user-invalid`** (`estilos.css`): antes cualquier campo
  vacío se marcaba en rojo desde el primer render, antes de que la persona escribiera
  nada. `:user-invalid` (CSS Selectors Level 4) solo se cumple después de que el campo
  fue tocado.
- **`.encabezado__nav` deja de heredar el estilo de los enlaces de texto corrido**:
  sin subrayado por defecto, color `--color-texto-tenue`, subrayado solo en
  `:hover`/`:focus-visible`. La página actual se marca con `aria-current="page"`
  (borde inferior en `--color-primario`), calculado por una función nueva
  registrada como global de Jinja (`app.portal.router._es_seccion_activa`, expuesta
  como `activa(request, *prefijos)`) en vez de que cada ruta tenga que pasarlo a mano
  -- compara `request.url.path` contra los prefijos de cada sección ("Mi carpeta"
  cubre `/carpeta` y `/documentos`, ya que el detalle de un documento sigue siendo
  parte de esa sección). El contador de notificaciones (`_notificaciones_contador.html`)
  es un caso aparte: se sirve por su propia petición HTMX a
  `/notificaciones/contador`, cuya URL nunca es la página que el ciudadano está
  viendo -- ahí `activa()` lee `HX-Current-Url` (la URL real del navegador, que HTMX
  manda siempre) en vez de `request.url.path`. Sin ese detalle, la insignia se habría
  marcado activa en cualquier pantalla, no solo en `/notificaciones`.
- **Enlace o botón, auditado**: se revisó cada `<a href>` del portal -- ninguno muta
  nada, todos navegan (descargar, sustituir, filtrar, paginar). Cada acción que
  cambia algo (eliminar un documento, marcar una notificación como leída, deshabilitar
  el segundo factor, confirmar un traslado) ya era un `<button>` dentro de un
  `<form method="post">`, en ambos lugares donde aparece Eliminar
  (`_documento_fila.html` y `documento.html`) -- no fue necesario corregir nada ahí,
  solo confirmarlo. Sí se ajustaron los *pesos* visuales de las acciones de un
  documento, que el documento pedía explícitamente: Descargar pasó de botón primario
  a `boton--secundario` (es la acción habitual); Sustituir y Eliminar pasaron de
  enlace/botón sólidos a `boton-enlace` (discretos, ocasionales), con Eliminar en rojo
  (`boton-enlace--peligro`, nueva clase) pero sin el bloque sólido de antes. Hallazgo
  aparte, fuera del alcance de esta tarea y no corregido: `totp.html` usa
  `boton--peligro` (rojo sólido) en "Deshabilitar segundo factor", que en sentido
  estricto viola el principio 1 del documento ("el rojo queda reservado para
  trasladarse y eliminar, nada más") -- preexistente, no introducido aquí.
- **La carpeta se separa en dos páginas**: `GET /carpeta` ya no trae el formulario de
  carga -- ahora solo la cabecera (título + cuota en una línea con su barra, ambas
  compactas vía `.carpeta-cabecera`/`.cuota-linea`, reutilizando `perfil_servicios.
  obtener_perfil` para el dato de cuota, que la ruta de la carpeta no consultaba antes),
  un botón "Subir documento" hacia `/carpeta/subir` (ruta nueva, `GET`), los filtros y
  la lista. Los filtros con menos uso (entidad, procedencia, fechas) quedaron dentro de
  un `<details>` cerrado ("Más filtros"), que se abre solo si alguno de ellos ya trae
  un valor -- título y tipo, los más usados, siguen siempre visibles. `POST
  /carpeta/documentos` no cambió de URL (los scripts de prueba existentes que postean
  ahí directo siguen funcionando sin tocarlos); lo que cambió es que ahora, tanto al
  mostrar el formulario vacío como al rechazar una carga con error, renderiza la
  plantilla nueva `carpeta_subir.html` en vez de `carpeta.html` -- esto además
  simplificó la ruta: ya no hace falta volver a listar los documentos solo para
  redibujar la página con el error.

Probado de punta a punta en Docker con un script ad-hoc (no incorporado a la
suite, solo para esta verificación): registro, login, `GET /carpeta` sin el
formulario inline y con botón/cuota/filtros avanzados/`aria-current`, `GET
/carpeta/subir` con el formulario y la navegación marcando igual "Mi carpeta" como
activa (por el prefijo `/carpeta`), una carga real completada desde la página nueva
con el redirect a `/carpeta?subido=1` y el aviso de éxito, y `/perfil` marcando su
propia sección sin marcar también "Mi carpeta". Como regresión completa (estos
cambios tocan rutas y no solo CSS, a diferencia de la pasada anterior): `prueba-portal`,
`prueba-portal-segunda-pasada`, `prueba-portal-primer-acceso` (con un token real) y la
suite JSON (`prueba-carpeta-completa`, `prueba-envio-transferencia`,
`prueba-reconciliacion` sus cuatro escenarios, `prueba-regreso-antes-de-purga`,
`prueba-primer-acceso` con un segundo token real) -- todas sin fallos, sin ajustar
ninguna aserción. No se probó a 390 px de ancho con un navegador real ni una captura
de pantalla (no hay herramienta de renderizado disponible en esta sesión): se revisó
por CSS que nada tiene un ancho fijo mayor al viewport disponible a ese tamaño
(`.cuota-linea .barra-cuota` son 160px, muy por debajo) y que los contenedores
flexibles relevantes (`.carpeta-cabecera`, `.encabezado__nav`, al que se le agregó
`flex-wrap: wrap` en esta misma tarea) permiten que el contenido se apile en vez de
desbordar -- es una revisión de código, no una verificación visual.

**Siete correcciones más al portal, encontradas usándolo (no leyendo el código),
aplicadas el 2026-09-24.** `docs/diseno.md` ya traía escritas las reglas detrás de
varias de estas correcciones -- señal de que se había avanzado en una sesión anterior
sin que el código llegara a aplicarlas del todo (el contenedor seguía en 1080px, no en
los 1120px que el documento ya pedía) -- así que el trabajo real de esta tarea fue
tanto corregir lo reportado como terminar de alinear el código con lo que el documento
ya describía, y limpiar dos artefactos de edición del documento mismo (un bloque de
CSS de "Cerrar sesión" duplicado dos veces seguidas, y una referencia residual a
`.contenedor--ancho` que la sección de ancho más nueva ya había vuelto innecesaria).

1. **El contenedor sube a 1120px** (`estilos.css`, `.contenedor`), no los 1080px de la
   pasada anterior -- ese número ya estaba en `docs/diseno.md` sin aplicar. La barra del
   encabezado usa la misma clase, así que queda alineada sin tocarla aparte. Los
   formularios de una sola columna (perfil, TOTP, traslado, notificaciones, detalle de
   un documento, sustituir, subir, primer acceso) siguen su propio tope --
   `.contenedor--estrecho` (640px, sin cambios de esta tarea), aplicado vía el bloque
   `main_extra_class` de `base.html` en cada una de esas nueve plantillas. La tarjeta
   partida de `sesion.html`/`registro.html` no necesita ese tope: a 1120px de ancho,
   `.entrada__forma` (55% del contenedor menos su padding) cae sola cerca de los 500px,
   ya angosta por su propio diseño de columnas.
2. **La cuota vuelve a tener su columna a la derecha** a partir de 1000px
   (`.carpeta-diseno`, grid de `1fr 300px`; por debajo de ese ancho se apila, la
   cuota después de la lista). Va en su propia tarjeta (`.carpeta-cuota .tarjeta`),
   separada de la lista, con su barra y la nota de MB usados. Esto reemplaza la
   `cuota-linea` de la pasada anterior (la línea junto al título "Mi carpeta"), que
   desaparece por completo. `docs/diseno.md` describía además una segunda tarjeta en
   esa misma columna, "Operador actual" con el enlace de traslado -- no estaba
   construida y se agregó en esta misma tarea: `GET /carpeta` ahora pasa
   `operador_actual` (`get_config().operator_name`, dato ya existente en `Config`,
   nunca escrito a mano en la plantilla) al contexto, y la tarjeta enlaza a
   `/perfil/traslado`, el mismo destino que ya existe en `/perfil`. No agrega lógica de
   negocio nueva: es un enlace más hacia una pantalla que ya existía.
3. **Los botones "Filtrar"/"Limpiar filtros" ya no se descuadran al abrir "Más
   filtros"** (`carpeta.html`): el bug real era que el `<form>` de los filtros tenía la
   clase `filtros` (un grid) directamente, así que el `<details>` y el `<div
   class="acciones">` de los botones eran también celdas de ese grid, y a partir del
   punto de quiebre de 2 columnas (640px) el grid los repartía de forma impredecible
   según si "Más filtros" estaba abierto o cerrado. Arreglado sacando `filtros` del
   `<form>` y envolviendo cada grupo de campos (el visible y el de dentro del
   `<details>`) en su propio `<div class="filtros">`: el `<details>` y `.acciones`
   quedan como hijos de bloque normales del `<form>`, nunca celdas de un grid, así que
   siempre ocupan su propia fila sin importar el estado del `<details>`.
4. **"Cerrar sesión" se ve igual que los demás elementos de la barra** (`estilos.css`,
   nueva regla `.encabezado__nav button`, misma receta que ya existía para
   `.encabezado__nav a`: sin fondo, sin borde, mismo color y peso). Sigue siendo un
   `<button>` dentro de un `<form method="post">` -- eso no cambia, cierra sesión y no
   puede ser un enlace -- se le quitó la clase `boton-enlace` (ya redundante: la regla
   nueva, más específica por combinar clase de contenedor y etiqueta, gana de todas
   formas) porque esa clase lo pintaba en azul y subrayado, distinto del resto de la
   navegación.
5. **El texto de ayuda ya no queda pegado al lado del botón**, en subir y en
   sustituir (`carpeta_subir.html`, `sustituir.html`). La causa: `.campo .ayuda` solo
   forzaba `display: block` cuando el `<span class="ayuda">` estaba anidado dentro de
   un `.campo` -- en esos dos formularios el texto de ayuda es hermano directo del
   botón, fuera de cualquier `.campo`, así que heredaba el `display: inline` por
   defecto de `<span>` y quedaba en la misma línea. Corregido generalizando la regla a
   `.ayuda` (sin el prefijo `.campo`): ahora es siempre un bloque propio, sin importar
   dónde esté anidado -- no hizo falta tocar ninguna plantilla para esto.
6. **Cédula y dirección de carpeta en `/perfil` pasan a campos de verdad**
   (`perfil.html`): antes eran una `<dl>` de texto suelto que no se distinguía del
   resto de la pantalla. Ahora son `<input readonly>` -- deliberadamente `readonly`,
   nunca `disabled`: un campo `disabled` no recibe foco ni lo anuncian los lectores de
   pantalla, así que nadie podría enterarse de cuál es su propia dirección de carpeta;
   `readonly` sí deja seleccionar y copiar el valor, que es exactamente lo que alguien
   va a querer hacer con ese dato. Aspecto apagado vía una regla nueva y genérica,
   `input[readonly]` (fondo `--color-suave`, texto tenue, cursor por defecto) -- no una
   clase aparte, así que cualquier otro campo de solo lectura que aparezca después
   hereda el mismo tratamiento sin que nadie tenga que acordarse de aplicarlo.
7. **El botón de eliminar un documento ya no se pone oscuro al pasar el cursor**
   (`estilos.css`, `.boton-enlace--peligro:hover`): antes heredaba el `background:
   var(--color-primario-oscuro)` de la regla genérica `button:hover` (más específica
   que el `.boton-enlace` base, que solo fija el fondo en reposo). Ahora
   `.boton-enlace--peligro` tiene su propio hover, más específico que la regla
   genérica: fondo `--color-error-fondo` (un rojo muy suave) con el texto en
   `--color-error-texto` -- discreto incluso al pasar el cursor, coherente con que
   Eliminar es una acción ocasional, no la principal de la fila.

Probado de punta a punta en Docker, reconstruyendo `app-a`/`app-b`: `prueba-portal`,
`prueba-portal-segunda-pasada` (con la tarjeta "Operador actual" y el enlace a
`/perfil/traslado` visibles en `/carpeta` durante la misma corrida) y
`prueba-portal-primer-acceso` (con un token real) -- las tres sin fallos. Como
regresión de la suite JSON (estos cambios tocan una ruta -- `GET /carpeta` ahora
también consulta `perfil_servicios.obtener_perfil` para la cuota, ya lo hacía desde la
pasada anterior -- pero ningún contrato de la API): `prueba-carpeta-completa`,
`prueba-envio-transferencia`, `prueba-reconciliacion` (los cuatro escenarios),
`prueba-regreso-antes-de-purga` y `prueba-primer-acceso` -- las cinco sin fallos.
Además, un script ad-hoc (no incorporado a la suite) verificó puntualmente cada una de
las siete correcciones sobre el HTML servido: ausencia de `.cuota-linea`, presencia de
`.carpeta-diseno`/`.carpeta-cuota`, el `<details>` cerrándose antes que el
`<div class="acciones">` en el HTML servido, ausencia de la clase `boton-enlace` en el
botón de salir, la regla `.ayuda` genérica (no `.campo .ayuda`) en el CSS servido, los
campos `readonly` en `/perfil`, y la regla `input[readonly]`/`.boton-enlace--peligro:hover`
en el CSS servido.

**No se verificó a 390px ni a 1400px con un navegador real ni una captura de
pantalla** -- sigue sin haber esa herramienta en esta sesión, igual que en la tarea
anterior. Se revisó por código: a 390px, `.carpeta-diseno` (grid de una sola columna
por debajo de 1000px) y `.encabezado__nav` (con `flex-wrap: wrap` desde la tarea
anterior) permiten que todo se apile sin desbordar; a 1400px, `.contenedor` simplemente
deja de crecer en 1120px (es un `max-width`, no un ancho fijo), así que no hay ningún
comportamiento nuevo que verificar ahí más allá de que quede centrado -- ninguno de los
dos es una confirmación visual real.

**Siete arreglos de interfaz más, encontrados usando el portal (no leyendo el
código), aplicados el 2026-09-24.**

1. **Las tres acciones de `/documentos/{id}` (Descargar, Sustituir, Eliminar) pasan a
   compartir geometría** -- antes Descargar era una caja con borde
   (`boton boton--secundario`) y Sustituir/Eliminar eran texto suelto
   (`boton-enlace`/`boton-enlace--peligro`), lo que obligaba a leer para saber que las
   tres se podían pulsar. Ahora las tres son `.boton` (mismo alto, relleno, radio y
   borde) con dos variantes nuevas que solo cambian el color: `boton--neutro`
   (Sustituir, gris) y `boton--peligro-discreto` (Eliminar, rojo sin relleno sólido).
   `boton--peligro` (relleno sólido) se queda igual que antes para una acción sola y
   grave sin nada al lado (trasladarse, deshabilitar el segundo factor) -- la
   diferencia de peso entre un grupo y una acción sola sigue siendo válida, lo que no
   vale es mezclar pesos *dentro* del mismo grupo. Auditadas todas las pantallas
   buscando el mismo problema: la fila compacta de un documento en la lista de la
   carpeta (`_documento_fila.html`) ya compartía geometría entre sus tres acciones
   (las tres en `boton-enlace`, solo Eliminar con su variante de color) desde la tarea
   anterior, así que no hizo falta tocarla -- ninguna otra pantalla tiene un grupo de
   dos o más acciones con formas distintas.
2. **`dl.detalle` (el detalle de un documento y el resultado de la firma) pasa a una
   rejilla de dos columnas** (`grid-template-columns: auto 1fr` a partir de 480px, una
   sola columna por debajo) en vez de etiqueta y valor apilados sin relación entre
   pares: ahora la columna de etiquetas mide lo que pida la más larga y todos los
   valores arrancan en la misma vertical, para las dos tarjetas que usan esa clase.
3. **Eliminar un documento y cerrar sesión piden confirmación con un `<dialog>` nativo
   propio** (`app/portal/static/confirmar.js`, nuevo) en vez del `window.confirm()` del
   navegador (que mostraba el dominio, "127.0.0.1:8000 dice", y no se podía estilar).
   Un solo diálogo compartido, inyectado una vez en `base.html`: Cancelar en neutro y
   con el foco inicial (para que `Enter` sin mirar no confirme nada), Confirmar en
   rojo. Dos caminos de enganche, sin tocar el atributo `hx-confirm` que ya existía:
   los formularios normales (cerrar sesión, eliminar desde el detalle -- este último
   no tenía NINGUNA confirmación hasta ahora, ni siquiera la nativa) llevan
   `data-confirmar="mensaje"` e interceptan su propio `submit`; el formulario con HTMX
   de la lista de documentos ya dispara `htmx:confirm` con el texto de su
   `hx-confirm`, así que basta escuchar ese evento una sola vez. **La acción nunca
   depende del diálogo**: sin JavaScript, cada formulario se sigue enviando de forma
   normal (verificado indirectamente: `scripts/probar_portal.py` usa `httpx`, que
   nunca ejecuta JavaScript, y su paso de eliminar sigue pasando -- ejercita
   exactamente el camino sin JS).
4. **La ayuda de subir y de sustituir pasa a estar ENCIMA del botón, no debajo.** La
   tarea anterior ya había arreglado que no quedara *al lado* del botón (generalizando
   `.ayuda` a `display: block` sin importar dónde estuviera anidada), pero el orden en
   el HTML seguía siendo botón-primero-ayuda-después, así que la ayuda quedaba debajo,
   pegada, en vez de encima con aire de sobra antes del botón como se pidió. Nueva
   clase `.ayuda--previa` (`margin: 0.25rem 0 1rem`) más el cambio de orden en las dos
   plantillas. La guía vieja en `docs/diseno.md` (que decía "primero el botón, después
   la explicación") estaba mal y quedó corregida.
5. **El filtro de operadores del traslado se consolidó en una sola función**,
   `url_transferencia_utilizable` (`app.interoperabilidad.traslado_servicios`), que
   ahora usan los tres lugares que antes tenían su propia copia del mismo chequeo:
   `listar_operadores_transferibles` (el desplegable), `solicitar_traslado` (la ruta
   que recibe la solicitud) y `app.interoperabilidad.outbox._enviar_transferencia` (el
   envío real). Descarta URL ausente o mal formada, esquema distinto de `https` (o de
   `http`/`https` si `TRANSFERENCIA_EXIGIR_HTTPS` está apagado) siempre, y -- **solo
   cuando `TRANSFERENCIA_EXIGIR_HTTPS` está prendido** -- hosts que no podrían ser
   públicos (`localhost`, `0.0.0.0`, `127.x.x.x`, cualquier host sin punto). El chequeo
   de host quedó condicionado a esa bandera porque aplicarlo siempre rompía
   `docker-compose.test.yml`: `test-operador-b` apunta a `http://app-b:8000/...`, un
   nombre de servicio de Docker sin punto, exactamente el patrón que se quería
   descartar del directorio real -- se encontró de la peor forma, haciendo fallar
   `prueba-portal-segunda-pasada` al primer intento ("test-operador-b todavia no
   aparece en el directorio"), y se corrigió reutilizando la misma bandera que el
   proyecto ya usa en todos lados para distinguir "directorio real" de "instancias de
   prueba sin TLS". `docs/especificacion.md` ya documentaba la respuesta a "¿si no
   queda ningún operador?" (`_documento_fila... No hay operadores disponibles...`
   ya existía en `traslado.html` desde la pasada anterior del portal): no hizo falta
   texto nuevo.

Probado de punta a punta en Docker: `prueba-portal`, `prueba-portal-segunda-pasada`
(que ejercita el filtro de operadores contra `test-operador-b` de verdad -- encontró y
dejó corregido el problema del punto 5 antes de darse por terminada la tarea) y
`prueba-portal-primer-acceso` (con un token real) -- las tres sin fallos tras la
corrección. Regresión JSON: `prueba-carpeta-completa`, `prueba-envio-transferencia`
(pasa también por `_enviar_transferencia`, el tercer lugar que ahora usa el filtro
compartido), `prueba-reconciliacion` (los cuatro escenarios), `prueba-regreso-antes-
de-purga`, `prueba-primer-acceso` y `prueba-firma-digital` -- las seis sin fallos. Se
verificó además, con un script ad-hoc contra el HTML/CSS servido, cada uno de los
siete puntos por separado (clases de botón unificadas, la rejilla de `dl.detalle`, el
diálogo y su script cargados, el orden ayuda-antes-que-botón, y la pantalla de
traslado respondiendo 200).

**Un hallazgo aparte, no un bug de esta tarea:** al generar rápidamente varios
ciudadanos de prueba con un PDF deliberadamente inválido (para otro chequeo manual),
uno de esos documentos hizo que `_validar_firma` registrara en el log un traceback
completo de pyHanko (`PdfReadError: startxref not found`) al no poder ni siquiera
abrir el archivo. La fila de `outbox` terminó igual `COMPLETADO` (no se cayó, no
bloqueó a otros ciudadanos) -- comportamiento correcto según el diseño ya documentado
("una firma inválida nunca es un rechazo"), solo con un log más ruidoso de lo
necessario para un caso ya anticipado (un archivo que ni siquiera es un PDF válido).
No se tocó nada al respecto: queda como una nota, no como algo corregido en esta
tarea.

**Lista de exclusión de operadores destino, agregada el 2026-09-24.** Nueva variable
`OPERADORES_EXCLUIDOS` (`Config.operadores_excluidos`, vacía por defecto; propiedad
`operadores_excluidos_ids` la separa por comas y recorta espacios), aplicada en
`listar_operadores_transferibles` -- **por encima** del filtro de URL utilizable de la
tarea anterior, y **solo** ahí: descarta operadores del desplegable por `_id`, nunca
por `nombre` (el directorio tiene nombres duplicados, CLAUDE.md "trampa 5"). Es una
decisión de operación propia, no una corrección del directorio -- documentado así en
docs/especificacion.md, "Directorio de operadores": el directorio sigue siendo la
fuente de verdad, esto solo filtra qué se le ofrece al ciudadano. Variable de entorno
en vez de una lista en el código, para poder sacar a alguien sin redesplegar el día
que arregle su operador. Agregada a `.env.example` con el valor vacío y su comentario.

**Deliberadamente no se aplica en `solicitar_traslado` ni en el envío real**
(`app.interoperabilidad.outbox._enviar_transferencia`): la instrucción de esta tarea
la limitaba explícitamente a "no se muestran en el desplegable", así que una solicitud
que ya trajera ese `_id` (por ejemplo, contra la API JSON directamente, sin pasar por
el desplegable del portal) no se rechaza por esto hoy. Si se quiere que la exclusión
también bloquee la solicitud en sí, falta agregarla ahí -- no se hizo porque no era lo
pedido, y hacerlo sin que se pidiera habría sido una decisión de alcance no
solicitada.

Probado en Docker contra `app-a`: con `OPERADORES_EXCLUIDOS` vacía (el valor por
defecto, sin declarar la variable) el desplegable de `/perfil/traslado` mostró los
mismos dos operadores de siempre (`test-operador-a`, `test-operador-b`) -- el
comportamiento no cambió. Editando puntualmente `OPERADORES_EXCLUIDOS=test-operador-a`
en el entorno de `app-a` de `docker-compose.test.yml` (cambio temporal, revertido
antes de terminar la tarea -- `git diff` sobre ese archivo queda limpio) y
reconstruyendo el contenedor, el mismo desplegable pasó a mostrar solo
`test-operador-b`. Como regresión con el valor vacío restaurado:
`prueba-portal-segunda-pasada` (ejercita el desplegable real y el traslado completo
hasta `CONFIRMADA`), `prueba-envio-transferencia` y `prueba-reconciliacion` (los
cuatro escenarios) -- las tres sin fallos.

**Cache-busting de los estáticos del portal, agregado el 2026-09-24 (encontrado en
producción el mismo día: el CSS servido seguía siendo el anterior tras un despliegue
hasta que alguien forzaba la recarga).** `StaticFiles` solo respondía con
`ETag`/`Last-Modified` -- un navegador que ya tuviera un archivo en caché no volvía a
pedirlo. Nuevo módulo `app/portal/estaticos.py`: `url_estatica(nombre)` calcula el
SHA-256 del contenido del archivo (los primeros 10 caracteres, cacheado en memoria por
proceso con `lru_cache`) y arma `/static/{nombre}?v={hash}` -- registrada como global
de Jinja `estatico` (`app.portal.router`), nunca escrita a mano: `base.html` la usa en
los siete lugares que apuntaban a `/static/...` (favicon, ícono del encabezado,
apple-touch-icon, manifiesto, hoja de estilos, HTMX, `confirmar.js`). Los otros dos
puntos que también apuntan a `/static/` sin ser un `<link>` de `base.html`:
`site.webmanifest` declara sus propios íconos con una URL fija adentro de su propio
contenido -- como no es una plantilla Jinja, no puede llamar a `url_estatica` por su
cuenta, así que `app/main.py` registra una ruta explícita
`GET /static/site.webmanifest` (antes del `mount` de abajo, para tener precedencia
sobre el archivo real del mismo nombre) que reescribe esas URL una sola vez
(`manifest_versionado`, también cacheada). `htmx.min.js` solo necesitaba pasar por
`estatico` como cualquier otro archivo.

**Corrección el mismo día, antes de llegar a producción: la cabecera `Cache-Control`
agresiva se enviaba a *todo* `/static`, incluidas las URL sin parámetro de versión.**
Sigue existiendo una: la redirección histórica `/portal/static/...`
(`app.portal.router.router_legado`) apunta a `/static/...` sin versión a propósito,
para no romper un enlace guardado con una versión que ya no existe. Cachear esa URL
sin versión durante un año habría sido *peor* que el problema original: una URL así sí
puede cambiar de contenido en el siguiente despliegue, y ya ni un despliegue nuevo la
corrige, porque nunca vuelve a pedirse. Corregido con
`app.portal.estaticos.cache_control_para(query_string)`: agresiva
(`public, max-age=31536000, immutable`) solo si la petición trae `?v=...`; sin eso,
`no-cache` (obliga a revalidar contra el `ETag` que `StaticFiles` ya pone -- sigue
evitando volver a bajar el archivo si no cambió, solo ya no evita la ida y vuelta de
red para preguntar). Aplicada tanto en `EstaticosVersionados.get_response` (el
`mount` de `/static`) como en la ruta explícita del manifiesto.

**El hash se calcula una sola vez por proceso.** En desarrollo con
`uvicorn --reload`, editar un archivo bajo `app/portal/static/` (CSS, JS, íconos) NO
dispara un reinicio automático -- `--reload` solo vigila archivos `.py` -- así que la
URL versionada seguiría siendo la de antes hasta reiniciar el proceso a mano. No es un
bug: es la misma razón por la que el cache-busting funciona en producción (un
despliegue nuevo es un proceso nuevo), pero conviene tenerlo presente para no
perseguir un cambio de CSS que "no aparece" en desarrollo.

Probado con `TestClient` (sin Docker, ya que es un chequeo de cabeceras HTTP puntual,
no un flujo de negocio): las siete URL de `base.html` salen versionadas, el
manifiesto servido trae sus íconos también versionados, `Cache-Control` sale agresivo
solo con `?v=...` (confirmado en `/static/estilos.css` con y sin el parámetro, y en
`/static/site.webmanifest` con y sin él) y `no-cache` sin él. Verificado además "tras
desplegar" reconstruyendo `app-a` en Docker: modifiqué `estilos.css`, reconstruí, y la
versión en la página pasó de `448dd63e1f` a `209517e85e` (y volvió a su valor original
al revertir el cambio de prueba). Regresión en Docker: `prueba-portal` (incluida la
redirección legada, que sigue devolviendo la URL sin versión tal cual se esperaba),
`prueba-portal-segunda-pasada`, `prueba-portal-primer-acceso` y
`prueba-carpeta-completa` -- las cuatro sin fallos.

**Estado de afiliación ante el MinTIC, visible en `/perfil`, agregado el 2026-09-24**
(hasta ahora el registro dejaba al ciudadano en `PENDIENTE_CENTRALIZADOR` sin que el
portal lo dijera en ningún lado). Nueva sección "Registro ante el MinTIC", ubicada
entre "Mis datos" y "Espacio usado", con el mismo patrón de sondeo HTMX que ya tenía
el estado de un documento (`_documento_estado.html`): `_perfil_afiliacion.html`
(nuevo fragmento) trae `hx-get="/perfil/afiliacion"`/`hx-trigger="load delay:5s"`
solo mientras hay algo que pueda cambiar solo, y deja de traerlo en cuanto se
resuelve. `app.identidad.perfil_servicios.estado_afiliacion` distingue tres casos --
`None` (ya `ACTIVO` o más allá: la sección muestra la confirmación y no vuelve a
sondear), `"pendiente"` (aviso en lenguaje llano de que se está confirmando el
registro, sigue sondeando) y `"fallido"` (la bandeja de salida agotó sus reintentos
con el `registerCitizen` de este ciudadano -- CU-01 dice que se queda en
`PENDIENTE_CENTRALIZADOR` para siempre en ese caso, así que sin esto el portal lo
dejaría pareciendo "pendiente" indefinidamente; se muestra y deja de sondear, igual
que un documento `RECHAZADO` por GovCarpeta) -- consultando la fila `registerCitizen`
más reciente para la cédula en `outbox` (mismo patrón exacto que
`app.documentos.servicios.firma_en_validacion`, con `Outbox.payload["cedula"].astext`
en vez de `documento_id`). `PENDIENTE_VERIFICACION` (antes incluso de la
Registraduría, ni siquiera llegó a encolar `registerCitizen`) no muestra nada en esta
sección -- deliberado: es una etapa distinta, no la que pidió esta tarea.

Probado de punta a punta en Docker: registré un ciudadano contra `app-a` y confirmé
que `/perfil` muestra el aviso "pendiente" con el `hx-get` presente antes de que la
bandeja de salida (`OUTBOX_INTERVALO_SEGUNDOS=3` en este entorno) alcance a
procesarlo, y que segundos después (sin recargar manualmente, pidiendo `/perfil` de
nuevo como lo haría el sondeo real) el mismo bloque ya muestra la confirmación sin el
atributo de sondeo. El caso `"fallido"` no ocurre solo contra `mock-centralizador`
(que siempre responde éxito), así que lo sembré directamente en la base desechable de
`app-a` (mismo patrón que `scripts/probar_reconciliacion.py`): retrocedí un ciudadano
ya `ACTIVO` a `PENDIENTE_CENTRALIZADOR` e inserté a mano una fila `registerCitizen`
`FALLIDO` para su cédula -- `/perfil` mostró el aviso de reintentos agotados
correctamente. Regresión: `prueba-portal` y `prueba-portal-segunda-pasada` (esta
última ejercita `GET`/`POST /perfil` de lleno, con la sección nueva en el camino) sin
fallos, más `prueba-carpeta-completa` confirmando que `GET`/`PATCH /api/v1/perfil` (la
ruta JSON, que no toca esta pantalla) sigue sin cambios.

**Consola de administración del operador (RF32-RF37, CU-22 -- ver AD-12 en
docs/especificacion.md), implementada y probada de punta a punta el 2026-09-24.** Antes
de esto no existía ninguna forma de ver el estado interno del sistema, y en particular
ninguna de observar que otro operador nos transfirió un ciudadano: CU-16 no deja una
fila propia en ninguna tabla, solo entradas de `outbox` y de `auditoria`. Nuevo paquete
`app/admin/` (`auth.py`, `servicios.py`, `router.py`, `templates/`), montado bajo
`/admin` en `app/main.py`, fuera del esquema OpenAPI y sin ningún enlace desde el
portal del ciudadano.

De solo lectura, sin excepción: ninguna pantalla tiene un botón que borre, reintente
ni edite nada. Nunca expone el contenido de un documento (ni un enlace de descarga ni
una URL firmada), y nunca muestra, ni siquiera truncado, un hash de contraseña, un
secreto TOTP, un token de primer acceso ni la clave de una entidad emisora -- los
`dataclass` de `app.admin.servicios` declaran explícitamente los campos que sí se
muestran, nunca pasan un modelo de SQLAlchemy completo a una plantilla.

Sin modelo de administradores: una sola credencial en `ADMIN_PASSWORD_HASH` (nueva
variable, vacía por defecto = consola inutilizable hasta configurarla), verificada con
la misma `app.identidad.seguridad.verificar_password` que ya usa el ciudadano. Sesión
propia (`app.admin.auth`): cookie `colcarpeta_admin_sesion` con `path=/admin` (aparte
de la cookie del ciudadano), mismas protecciones que el portal
(`HttpOnly` + `Secure` + `SameSite=Strict`), JWT independiente del de
`app.identidad.token` (mismas llaves RS256, claim `sub` fijo en `"admin"`, sin ningún
`Ciudadano` detrás). Límite de intentos por origen, mismo patrón que el bloqueo de
inicio de sesión del ciudadano (`app.identidad.servicios._intentos_fallidos`, derivado
de `auditoria`, sin tabla propia): 5 intentos en 15 minutos por defecto, mismas
variables `INTENTOS_LOGIN_MAXIMOS`/`INTENTOS_LOGIN_VENTANA_MINUTOS`/
`BLOQUEO_LOGIN_MINUTOS` que ya existían. Cada acceso (login fallido, bloqueo, login
exitoso, y cada pantalla vista) queda en `auditoria` con acciones propias
(`admin.credenciales_invalidas`, `admin.bloqueado`, `admin.sesion_exitosa`,
`admin.pantalla_vista`).

Las cinco pantallas pedidas: **Resumen** (ciudadanos/documentos por estado,
transferencias salientes por estado, bandeja de salida pendientes/fallidas -- este
resumen agrupa `PENDIENTE` y `EN_PROCESO` de `outbox` bajo "pendientes", una
interpretación propia: el pedido solo distinguía pendientes de fallidas, no las tres
categorías por separado), **Ciudadanos** (cédula, nombre, estado, fecha de registro,
documentos y cuota usada, buscable por cédula parcial), **Transferencias** (entrantes y
salientes en una sola vista -- ver más abajo), **Bandeja de salida** (operación,
estado, intentos, último error, próximo intento) y **Auditoría** (filtrable por
cédula, acción y rango de fechas, paginada -- deliberadamente sin mostrar la columna
`detalle` de ningún evento: auditar cada punto del código que escribe ahí para
garantizar que ninguno registra algo sensible resultaba más riesgoso que simplemente no
exponerla nunca desde una consola de solo lectura).

La pantalla de transferencias es la única que exigió una decisión de diseño real:
`Transferencia` (tabla) solo existe para el envío (CU-03) -- una recepción (CU-16)
nunca crea una fila ahí, solo una entrada de `outbox` con `operacion =
"receiveTransferCitizen"`. `app.admin.servicios.listar_transferencias` une las dos
fuentes en Python (no en una sola consulta SQL, dado lo distinto de su forma) y
resuelve el operador de una entrante por una heurística nueva e independiente
(`_resolver_operador_por_confirm_api`, en el propio módulo): compara el host de la URL
de `confirm_api` que trae la transferencia contra el host de `transfer_api_url` de cada
operador del directorio. Deliberadamente NO reutiliza `_origen_coincide` de
`app.interoperabilidad.transferencias` (esa función es un control de seguridad para un
propósito distinto; acoplar un módulo de reporte de solo lectura a ella se sintió como
el acoplamiento equivocado) -- nunca es una identidad confirmada, el ecosistema no
tiene autenticación real entre operadores (ver "Trampas del contrato del
centralizador", trampa 6).

**Hallazgo, no de código:** el pedido original citaba "CU-19" para esta consola; la
propia tabla de casos de uso de `docs/especificacion.md` la cataloga como **CU-22**
("Operar consola de administración", RF36) -- CU-19 ("Consultar auditoría de accesos")
es un caso de uso distinto, del lado del ciudadano (que un ciudadano vea quién accedió
a sus propios documentos), que sigue sin implementar. La consola y su AD (AD-12,
docs/especificacion.md) quedaron documentadas con la referencia correcta; se avisó en
el reporte de la tarea para que quien la pidió lo supiera.

Probado de punta a punta en Docker (`docker-compose.test.yml`, `app-a`/`app-b` con
`ADMIN_PASSWORD_HASH` agregado de forma temporal a su entorno de prueba y revertido
antes de terminar -- `git diff --stat docker-compose.test.yml` quedó limpio): sin
cookie, las cinco pantallas redirigen a `/admin/login` (303); `/admin` no aparece en
`/openapi.json` (se confirmó contra el esquema real, que sigue en 19 rutas de la API);
login con clave incorrecta cinco veces seguidas bloquea el sexto intento **incluso con
la clave correcta** (confirmando que el bloqueo es real, no solo un mensaje); con la
cookie, las cinco pantallas responden 200 con datos reales -- se corrieron
`scripts/probar_carpeta_completa.py` y `scripts/probar_envio_transferencia.py` primero
para tener ciudadanos, documentos y una transferencia real que mostrar, y se confirmó
que `app-b` (el receptor) muestra la transferencia entrante con dirección `ENTRANTE`,
estado `RECIBIDA` y el operador de origen resuelto correctamente (`Operador A
(prueba)`) -- exactamente el escenario que motivó la tarea. Se buscó
(`argon2`, `X-Amz-`, `s3.`, `supabase`, `token_primer_acceso`, `totp_secret`,
`api_key`, `password_hash`, `signature`, enlaces `.pdf`/`descarga`) en el HTML de las
diez páginas capturadas (cinco pantallas × dos apps): ninguna coincidencia. No se probó
`ADMIN_PASSWORD_HASH` vacío contra un servidor vivo (se verificó por lectura de código:
`verificar_password(None, password)` siempre devuelve `False`, y
`cfg.admin_password_hash or None` convierte la cadena vacía por defecto en `None`).

**Dos ajustes más a la consola, el mismo 2026-09-24.** Primero, cerrar sesión en la
consola pide confirmación con el mismo `<dialog>` nativo que ya usa el portal
(`app/portal/static/confirmar.js`, reutilizado tal cual): `base_admin.html` agrega el
mismo diálogo compartido y el mismo atributo `data-confirmar` en el formulario de
salir, visible solo en las pantallas con sesión (la de login no lo necesita). Segundo,
un enlace de vuelta al portal en la cabecera de la consola (`/`), para no tener que
escribir la ruta a mano al salir de ahí. Se había agregado también un enlace discreto
en el pie del portal hacia `/admin` ("Acceso del operador"), probado el mismo día, y se
quitó a pedido explícito poco después: cualquier enlace hacia la consola visible en una
pantalla que ve cualquier ciudadano -- por discreto que sea -- anuncia su existencia y
su ruta exacta a un público mucho más amplio que quien la necesita, sin ninguna
necesidad real (quien opera el sistema ya conoce la ruta). Documentado como un matiz de
AD-12 (docs/especificacion.md): el enlace que sí queda (consola → portal) no relaja
ningún control de la consola. Probado en Docker: el diálogo de confirmación aparece en
`/admin/` y no en `/admin/login`; el enlace de vuelta al portal (`/`) funciona desde la
cabecera de la consola; el portal ya no tiene ningún enlace hacia `/admin` en ningún
lado.

**Origen del ciudadano como dato propio, con migración real aplicada el 2026-09-24.**
Cuatro columnas nuevas en `ciudadano` -- `origen` (`REGISTRO_DIRECTO` | `TRANSFERENCIA`,
enum `origen_ciudadano`), `origen_confirm_api`, `origen_operador_id`,
`origen_operador_nombre` -- fijadas una sola vez al crear la fila y nunca modificadas
después (migración `b1fb337fb116`, generada con `alembic revision --autogenerate`
contra la base real de Supabase y **aplicada a esa misma base real** con
`alembic upgrade head`, siguiendo el patrón ya usado en toda la sesión). Los
ciudadanos que ya existían quedan con `origen` en `NULL` -- deliberadamente sin
backfill, nunca adivinado.

Se fija en dos lugares, cada uno responsable de su propio origen: `REGISTRO_DIRECTO` en
`app.identidad.servicios.registrar_ciudadano` (CU-01, en los dos puntos donde se crea
la fila -- el camino básico y la reserva en `PENDIENTE_VERIFICACION` de la E3; la rama
que reutiliza un `existente` no lo toca, porque ya lo tiene desde su creación).
`TRANSFERENCIA` en `app.interoperabilidad.outbox._recibir_transferencia` (CU-16, paso
2, el único lugar donde se crea un `Ciudadano` recibido), junto con la nueva función
`_resolver_operador_origen`: compara el host de `confirmAPI` contra `operador_cache` en
ese mismo instante y guarda `_id` y nombre como fotografía -- nunca una relación viva
hacia `operador_cache`, cuyo nombre puede cambiar en un refresco posterior. Ninguna
recuperación (CU-03/CU-16 fallidos, que reutilizan `registerCitizen` vía
`_activar_ciudadano`) pasa por `registrar_ciudadano`, así que no hay riesgo de que un
ciudadano recuperado se reclasifique como `REGISTRO_DIRECTO` por error.

**Honestidad del dato, documentada como una carencia del acuerdo entre equipos, no
propia** (docs/especificacion.md, nueva sección "Ausencia de un identificador del
operador de origen", dentro de "Interoperabilidad entre operadores"): el formato de
transferencia acordado no incluye ningún campo que identifique al operador remitente,
así que `origen_operador_id`/`nombre` son siempre una deducción a partir del host de
`confirmAPI`, nunca un dato recibido -- la misma clase de heurística que ya usa
`_origen_coincide` para la confirmación de un envío propio (CLAUDE.md, trampa 6). La
consola de administración refleja esa honestidad al mostrarlo: `app.admin.servicios
._origen_mostrar` compone "Registro directo", "Transferido desde {nombre}" (cuando se
resolvió contra el directorio), "Transferido desde {host} (sin resolver en el
directorio)" (cuando no se pudo resolver, mostrando el host tal cual en vez de
inventar un nombre) o "Desconocido" (ciudadano previo a la migración) -- nunca presenta
una deducción como si fuera un dato certificado.

Pantalla de Ciudadanos de la consola (`app/admin/templates/ciudadanos.html`) extendida
con la columna "Origen" (el texto de arriba), un filtro por origen (`REGISTRO_DIRECTO`
/ `TRANSFERENCIA` / `DESCONOCIDO`, este último `origen IS NULL`) y una columna
"Operador destino" -- esta última **certera, no deducida**: para un ciudadano
`TRASLADADO`, se resuelve contra `Transferencia.operador_destino_id` (el operador que
ColCarpeta mismo eligió al enviarlo, `estado == CONFIRMADA`), nunca por heurística de
host. Deliberadamente **no** se deriva ningún origen desde la bandeja de salida: esa es
una cola de trabajo transitoria (las filas se purgan o quedan `FALLIDO`/`COMPLETADO`
sin ninguna garantía de retención histórica), no un registro pensado para consultarse
meses después -- exactamente lo que pedía esta tarea. El heurístico existente de
`app.admin.servicios` (pantalla de Transferencias, para las filas `ENTRANTE` derivadas
de `outbox`) sí se tocó, al día siguiente (ver "Unificación de los dos resolutores de
operador" más abajo): en la primera versión de esta tarea se dejó como una copia
independiente del nuevo `_resolver_operador_origen` de `outbox.py`, razonando que
resolvían problemas distintos -- un error real, corregido tan pronto se señaló: las dos
funciones respondían exactamente la misma pregunta ("qué operador está detrás de esta
URL"), y dos implementaciones de la misma pregunta pueden divergir con el tiempo.

Probado de punta a punta en Docker con el ciclo completo entre dos instancias
(`postgres-a`/`postgres-b`/`mock-centralizador`/`app-a`/`app-b`, con
`ADMIN_PASSWORD_HASH` agregado de forma temporal a ambas instancias y revertido antes
de terminar -- `git diff --stat docker-compose.test.yml` quedó limpio):
`scripts/probar_envio_transferencia.py` registró un ciudadano en `app-a` y lo trasladó
a `app-b`; la consola de `app-a` mostró ese ciudadano con origen "Registro directo" y
operador destino "Operador B (prueba)" (cierto, de la tabla `Transferencia`); la
consola de `app-b` mostró al mismo ciudadano recibido con origen "Transferido desde
Operador A (prueba)" (deducido, resuelto contra el directorio) y sin operador destino
(sigue afiliado ahí). El filtro por origen se probó en las tres variantes
(`TRANSFERENCIA` trae 1 en `app-b`, `REGISTRO_DIRECTO` trae 0 ahí, `DESCONOCIDO` trae 0
en `app-a` con datos reales). Se sembró a mano un ciudadano sin `origen` (`NULL`,
simulando uno anterior a la migración) directamente en `postgres-a` -- la consola lo
mostró como "Desconocido" y el filtro `DESCONOCIDO` lo encontró; se limpió después de
la prueba, en una base desechable. Regresión completa contra `app-a`, para confirmar
que fijar `origen`/`origen_confirm_api`/`origen_operador_id`/`origen_operador_nombre`
en las dos rutas de creación no rompió nada: `scripts/probar_carpeta_completa.py`,
`scripts/probar_reconciliacion.py` (los cuatro escenarios), `scripts/probar_colision_email.py`,
`scripts/probar_regreso_antes_de_purga.py` (ejercita el mismo paso 2 de
`_recibir_transferencia` que ahora resuelve el origen) y `scripts/probar_firma_digital.py`
-- las seis sin fallos.

**Hallazgo aparte, no de este cambio:** `app/admin/templates/ciudadanos.html` apareció
modificado en el disco durante esta tarea (fuera de esta sesión) con el botón "Limpiar"
del filtro convertido de un `<a href="/admin/ciudadanos">` a un
`<button type="button" onclick="window.location.href=...">`. Se dejó tal cual -- no era
parte de lo pedido y el archivo ya venía así -- pero vale la pena señalarlo: el resto
del proyecto evita a propósito depender de `onclick` para navegar (ver "Enlace o botón,
auditado" más arriba, sobre el portal), precisamente porque un enlace corriente
funciona sin JavaScript, se puede abrir en una pestaña nueva y se puede copiar, y un
`onclick` no. Si se quiere, revertirlo a un `<a>` es una línea.

**Unificación de los dos resolutores de operador, el 2026-09-24.** Señalado
correctamente en revisión: `app.interoperabilidad.outbox._resolver_operador_origen`
(nuevo el día anterior, para guardar `Ciudadano.origen_operador_id`/`nombre`) y
`app.admin.servicios._resolver_operador_por_confirm_api` (ya existente, para la
columna "Operador" de las filas `ENTRANTE` en la pantalla de Transferencias) hacían
exactamente la misma pregunta -- "qué operador del directorio corresponde a esta URL de
`confirmAPI`, por coincidencia de host" -- con dos copias del mismo bucle. La
justificación original ("resuelven problemas distintos") no resistía el argumento
correcto: la pregunta es una sola, y dos implementaciones de la misma pregunta pueden
divergir con el tiempo -- la consola mostrando un operador y el dato guardado en
`ciudadano` mostrando otro, para la misma transferencia.

Unificadas en `app.interoperabilidad.transferencias.resolver_operador_por_host`
(`operadores: list[OperadorCache], url: str | None) -> OperadorCache | None`), que vive
junto a `_resolver_host`, ya existente en ese mismo módulo. Los dos consumidores ahora
son capas delgadas sobre esa única función: `outbox._resolver_operador_origen` extrae
`(id, nombre)` del `OperadorCache` que devuelve (o `(None, None)`);
`admin.servicios._mostrar_operador_entrante` (renombrada, ya no pretende resolver nada
por sí misma) solo le agrega el formato de texto que pide esa pantalla. La diferencia
real que sí queda separada -- y que el código ahora dice explícitamente, en el
docstring de `resolver_operador_por_host` -- es `_origen_coincide`, que verifica *quién
llama* comparando IPs resueltas por DNS, no *qué URL quedó guardada*: una pregunta
distinta, no una copia de esta.

Sin migración ni cambio de comportamiento visible: mismo algoritmo exacto, ahora en un
solo lugar. Probado en Docker con el mismo ciclo de `probar_envio_transferencia.py`
(`app-a`/`app-b`/`mock-centralizador`, `ADMIN_PASSWORD_HASH` temporal revertido después):
la consola de `app-b` siguió mostrando "Transferido desde Operador A (prueba)" para el
ciudadano, y la pantalla de Transferencias de `app-b` siguió mostrando "Operador A
(prueba)" para la fila `ENTRANTE` correspondiente -- ambas lecturas del mismo dato,
ahora imposibles de hacer divergir porque comparten la función. Regresión:
`scripts/probar_carpeta_completa.py` y `scripts/probar_reconciliacion.py` (los cuatro
escenarios) sin fallos.

**La plantilla `app/admin/templates/ciudadanos.html` se restauró una tercera vez el
2026-09-24**, tras confirmarse (con `cat` directo al archivo y `git diff --no-index`
contra vacío, antes de tocar nada) que había vuelto en disco a la versión sin la
columna "Origen", el filtro por origen y la columna "Operador destino" -- la causa más
probable, señalada por quien pidió el cambio: tener el archivo abierto en un editor
cuyo guardado pisó el trabajo de la sesión. La lógica de fondo
(`app.admin.servicios.listar_ciudadanos`, `FilaCiudadano.origen`/`operador_destino`)
nunca se había perdido, solo la plantilla. Restaurada y confirmada esta vez con
`git diff` real (el directorio `app/admin/` ya quedó indexado por git en algún punto
entre una revisión y la siguiente, así que ahora sí muestra diffs por archivo en vez de
aparecer como directorio nuevo completo) -- el diff mostró exactamente las tres piezas
agregadas y nada más. Probado de nuevo en Docker con el mismo ciclo de
`probar_envio_transferencia.py`: columna, filtro y operador destino visibles y
correctos en `app-a` y `app-b`.

**Backfill de `origen` para los cuatro ciudadanos previos a la migración de esquema,
aplicado a la base real el 2026-09-24 (migración de datos `567f21d2ce7a`).** Antes de
escribir nada se consultó la base real, de solo lectura: cuatro ciudadanos con
`origen IS NULL` (`1122334455`, `1077700123`, `1032123123`, `1034556781`, todos
creados antes de que `b1fb337fb116` agregara la columna), y **una sola** fila en
`outbox` con `operacion = 'receiveTransferCitizen'` en toda la historia de la base
(`outbox` no tiene purga automática, así que esa es la historia completa, no una
muestra) -- lo que, por instrucción explícita, obligaba a parar antes de escribir
cualquier cosa y reportarlo primero, en vez de asumir que "una sola fila" significaba
"no hay nada que revisar". Esa fila (`id 42`, `FALLIDO`, cédula `1234567890`) se
investigó a fondo antes de concluir nada: no corresponde a ningún ciudadano existente
(esa cédula no está en la tabla), su `confirm_api` (`http://prueba-transferencia:9302/...`)
es un nombre de host que solo existe dentro de la red aislada de Docker de
`docker-compose.test.yml` -- imposible de haber originado fuera de esta máquina --, y
el endpoint de transferencia de este operador nunca se publicó ante el MinTIC
(CLAUDE.md, "Prohibido"), así que ningún operador real pudo haber sabido que
ColCarpeta existía como destino. Confirmado como residuo de
`scripts/probar_transferencia.py` corrido contra esta misma base durante "Probar en
Linux" (2026-09-21/22), se fijó `origen = REGISTRO_DIRECTO` en los cuatro ciudadanos
verificados -- por `id` explícito, nunca por un `WHERE origen IS NULL` genérico, para
que la migración no toque ninguna fila que no haya sido comprobada una por una. El
razonamiento completo, con cada consulta que lo respalda, queda escrito en el propio
archivo de la migración (`alembic/versions/567f21d2ce7a_...py`) para que se pueda
reconstruir sin esta conversación delante. Verificado tras aplicarla: los cuatro
ciudadanos quedaron en `REGISTRO_DIRECTO`, cero siguen en `NULL`.

**El sujeto del certificado en el detalle de un documento se traduce a lenguaje
llano, y la rejilla de esa pantalla se corrigió, el 2026-09-24.** Antes,
`documento.firma_firmante` (la cadena técnica de `asn1crypto.x509.Name.human_friendly`,
p. ej. `"Common Name: Secretaria General, Organization: ..., Country: CO"`) se
mostraba tal cual, y además tres datos distintos (la explicación de validez, el
firmante y la fecha) iban apretados dentro de un solo `<dd>` con `<br>` -- ni
traducido ni alineado con el resto de la pantalla. Nuevo módulo `app/portal/paises.py`
(tabla ISO 3166-1 alpha-2 → español completa, `nombre_pais(codigo)`, cae al código tal
cual si no lo reconoce -- nunca inventa un nombre) y `app/portal/router._firmante_legible`
(con `_campos_sujeto_certificado`, que separa la cadena por sus campos usando el mismo
separador que `asn1crypto` elige, `", "` o `"; "` según si algún valor ya trae una
coma). Si no reconoce ninguno de los tres campos esperados (`Common Name`,
`Organization`, `Country`), cae a mostrar la cadena completa como "Firmante": no
pierde el dato, pero tampoco pretende haberlo entendido. `_documento_estado.html`
(CU-09) reemplaza el `<dd>` con `<br>` por una explicación en su propio párrafo, tres
filas nuevas (Firmante/Entidad/País) en la MISMA rejilla `dl.detalle` que ya usaba la
pantalla (nunca una aparte), más Fecha de la firma, y el texto crudo del certificado
detrás de un `<details>` cerrado ("Ver los datos técnicos del certificado") -- nunca
se pierde el dato exacto, solo deja de ser lo primero que se ve. `.filtros-avanzados
summary` (la única pieza de "sección colapsable" que ya existía, usada hasta ahora
solo por "Más filtros" en la carpeta) se generalizó a un `summary` sin calificar, para
que el `<details>` nuevo herede el mismo resorte visual sin inventar una regla
paralela. Regla nueva en `docs/diseno.md` (sección 1 y sección 5, "Datos que vienen de
un sistema externo se traducen antes de mostrarse"): certificados, códigos de país y
estados de una API ajena se traducen siempre, con el dato crudo conservado pero nunca
destacado.

Auditado el resto de la pantalla en busca del mismo patrón (revisión pedida
explícitamente, no solo el bloque de la firma): el otro `dl.detalle` de
`documento.html` (Tipo/Entidad emisora/Fecha de emisión/Cargado) ya usaba un `dt`/`dd`
por dato, sin mezclar; `grep` confirmó que `<br>` dentro de un `<dd>` solo aparecía en
el bloque de la firma en todo `app/portal/templates/` y `app/admin/templates/`, y que
`dl.detalle` solo se usa en esos dos lugares más `app/admin/templates/resumen.html`
(que ya respeta la rejilla) -- no había un segundo caso que corregir.

Probado con pyHanko real, no solo con cadenas de ejemplo escritas a mano: se generó un
certificado autofirmado con `Common Name`, `Organization` y `Country=CO` (mismo patrón
que `scripts/probar_firma_digital.py`, extendido con los otros dos atributos), se
firmó un PDF de verdad, se validó con `app.documentos.firma.validar_firma_pdf`, y el
`firma_firmante` real que produjo -- `"Common Name: Secretaria General (certificado de
prueba), Organization: Entidad Emisora de Pruebas ColCarpeta, Country: CO"` -- se pasó
por `_firmante_legible`, dando exactamente `{"firmante": "Secretaria General
(certificado de prueba)", "entidad": "Entidad Emisora de Pruebas ColCarpeta", "pais":
"Colombia"}`. Confirmado también con el certificado de un solo campo que ya usa
`scripts/probar_firma_digital.py` (solo `Common Name`): cae correctamente a mostrar
únicamente la fila "Firmante", sin "Entidad" ni "País" (ninguno de los dos existe), sin
error. Renderizado en aislamiento con Jinja2 (sin Docker) para cuatro casos --
firmado con los tres campos, firmado con solo `Common Name`, sin firma, firma
inválida con un código de país sin reconocer (`XX`, mostrado tal cual) -- los cuatro
sin errores de plantilla. Probado de punta a punta en Docker contra `app-a` viva: se
subió el PDF firmado real (con los tres campos) por el formulario de la carpeta, se
esperó a que la bandeja de salida completara `validarFirma`, y se confirmó en el HTML
servido de verdad que aparecen las filas "Firmante", "Entidad" y "País: Colombia" en
`dl.detalle`, y el sujeto completo dentro del `<details>` -- sin ningún `<br>` suelto
ni el patrón viejo "Firmante declarado: ...". Regresión:
`scripts/probar_firma_digital.py` y `scripts/probar_portal.py` (que ejercita el
detalle de un documento sin firma) sin fallos.

**No se verificó a 390 px con un navegador real ni una captura de pantalla** -- sigue
sin haber esa herramienta en esta sesión. Se revisó por código: la rejilla nueva
reutiliza el mismo `dl.detalle` (una sola columna por debajo del umbral, `dt` y `dd`
apilados, `row-gap: 0.6rem`) que ya usa el resto de la pantalla. Los valores más largos
("Entidad Emisora de Pruebas ColCarpeta") no tienen `white-space: nowrap` en ningún
punto de la cadena de estilos, así que envuelven en vez de desbordar. **El umbral en sí
quedó corregido al día siguiente** (ver el punto que sigue): a 480px, justo donde
antes cambiaba a dos columnas, apenas quedaban ~200px para el valor -- ni siquiera
ese ancho alcanzaba a evitar el corte incómodo que esta misma tarea quería resolver.

**`dl.detalle` pasa a apilarse por debajo de 800px, no 480px, corregido el
2026-09-25 tras señalarse que 480px no alcanzaba.** Con una columna de etiquetas de
ancho fijo (`auto`), el espacio que le quedaba al valor justo al cruzar los 480px
-- unos 200px, contando el padding de `.contenedor--estrecho` y de `.tarjeta` -- seguía
siendo insuficiente para un nombre largo como "Entidad Emisora de Pruebas ColCarpeta"
(el mismo caso real de la tarea anterior), que se partía mal. Nuevo umbral: 800px,
igual al que ya usa `.entrada` (sección 8 de docs/diseno.md) para pasar de dos
columnas a una -- reutilizado a propósito, no inventado, siguiendo el mismo patrón que
"si ya existe una pieza para esto, úsala" venía aplicando el resto de esta sesión.
Cambio de un solo número en `app/portal/static/estilos.css`
(`@media (min-width: 480px)` → `@media (min-width: 800px)`) más la actualización del
mismo bloque en `docs/diseno.md`; ninguna plantilla cambió. Afecta a los tres lugares
que comparten `dl.detalle` (`documento.html`, `_documento_estado.html`,
`app/admin/templates/resumen.html`): los tres se vuelven más conservadores (se apilan
en un rango de anchos donde antes iban en dos columnas), nunca menos legibles.
Verificado en Docker que el CSS servido trae el nuevo umbral
(`curl .../static/estilos.css | grep -A2 dl.detalle`) y que `scripts/probar_portal.py`
sigue sin fallos (ejercita el detalle de un documento, que usa esta misma rejilla). No
se verificó con un navegador real a 390px ni a un ancho justo por encima de 800px --
misma limitación de herramientas que el resto de esta sesión.

**Colisión entre la etiqueta "Certificado"/"Temporal" y el tipo de documento,
corregida el 2026-09-24 (rehecha ese mismo día: el primer intento se perdió del
disco antes de llegar a `git commit` -- ver la nota al final de este punto).** El
adjetivo también es un sustantivo común que el ciudadano ya usaba para otra cosa: el
tipo de documento lo escribe él mismo a mano ("certificado laboral", "certificado de
estudios"), así que un mismo documento podía leerse "Certificado" en el tipo y
"TEMPORAL" en la etiqueta unas líneas más abajo, o "CERTIFICADO" dos veces con
significados distintos. Las etiquetas de `app.portal.templates._macros.etiqueta_procedencia`
pasan a nombrar el origen en vez de una cualidad: "De una entidad" (antes
"Certificado") y "Subido por ti" (antes "Temporal") -- ninguna de las dos coincide con
un tipo que alguien pueda escribir a mano. Deliberadamente **sin tocar** el campo
`documento.certificado` del modelo, la API, ni el vocabulario de
`docs/especificacion.md`: ahí "certificado" es el contrato y el lenguaje del caso de
estudio, sin ambigüedad porque no convive con un tipo escrito por el ciudadano. El
filtro de procedencia en `/carpeta` usa las mismas dos palabras. La macro
`nota_procedencia` ("Información proporcionada por ti", debajo de un documento
temporal) se eliminó por completo, junto con sus dos llamados
(`documento.html`, `_documento_fila.html`): con la etiqueta nueva decía lo mismo dos
veces.

Aprovechando que se estaba tocando `_documento_fila.html`, se corrigió también que
Descargar/Sustituir/Eliminar en la lista de la carpeta seguían siendo texto subrayado
de distinto color y tamaño -- el arreglo de "las acciones de un grupo comparten
geometría" (aplicado antes solo al detalle de `/documentos/{id}`) llega ahora también
aquí: las tres pasan de `.boton-enlace`/`.boton-enlace--peligro` a `.boton`
completo con sus variantes de color ya existentes (`boton--secundario`,
`boton--neutro`, `boton--peligro-discreto`), sin agregar ninguna clase CSS nueva --
ya existían, solo no se usaban ahí. Causa raíz de por qué `boton-enlace` nunca
funcionó para esto, documentada en `docs/diseno.md`: no fija su propio `min-height`,
así que un `<a class="boton-enlace">` (Descargar, Sustituir) queda con la altura de su
padding y su texto, mientras que el `<button class="boton-enlace ...">` de Eliminar
(tiene que ser un botón porque hace POST) sigue emparejando con el selector genérico
`button, .boton { min-height: 44px; ... }` -- las tres acciones de la misma fila
terminaban con alturas distintas.

Nueva regla en `docs/diseno.md` (sección 1 "Principios" y sección 4 "Etiquetas de
estado"): una etiqueta de estado no puede ser una palabra que la persona también
pueda escribir como dato; si puede, se nombra la acción o el actor detrás del estado,
no la cualidad. `scripts/probar_portal.py` actualizado: las dos aserciones viejas
("Temporal" y "Información proporcionada por ti") se reemplazan por una sola
("Subido por ti").

**Nota sobre por qué esto se rehizo dos veces.** La primera versión de este mismo
cambio (incluida una corrección aparte, el bloque `.documento-encabezado` que empareja
el título de un documento con su etiqueta en la misma línea en vez de una encima de la
otra) se implementó y se probó de punta a punta en una sesión anterior, pero nunca
llegó a un `git commit` -- y para cuando se pidió rehacerla, ya no estaba ni en el
disco ni en el historial de git (`git status`/`git log` limpios, sin rastro). La causa
más probable, ya vista antes con `app/admin/templates/ciudadanos.html`: un editor con
una pestaña vieja de estos mismos archivos abierta, que sobrescribió el trabajo al
guardar. Al detectarlo, se restauraron **ambos** cambios juntos -- el de la etiqueta
(pedido explícitamente esta vez) y el de `.documento-encabezado` (no pedido esta vez,
pero indispensable: rehacer uno sin el otro habría reintroducido en el mismo archivo
un bug de layout ya diagnosticado y corregido antes).

Probado de punta a punta en Docker: `scripts/probar_portal.py` sin fallos; un script
ad-hoc (no incorporado a la suite, borrado al terminar) subió un documento con
`tipo="Certificado laboral"` literal y confirmó la etiqueta nueva sin colisión visible,
las tres acciones de la fila compartiendo clase `.boton`, el filtro con las opciones
nuevas, la nota redundante ausente, y el `<h1>` + la etiqueta en el mismo bloque
`.documento-encabezado` en el detalle. Regresión: `scripts/probar_portal_segunda_pasada.py`
(traslado completo hasta `CONFIRMADA` incluido) y `scripts/probar_carpeta_completa.py`
-- ambas sin fallos. No se verificó a 390 px con un navegador real ni una captura de
pantalla -- misma limitación de herramientas que el resto de esta sesión; se revisó por
código que `.documento-fila__acciones`, `.acciones` y `.documento-encabezado` tienen
`flex-wrap: wrap`, así que las acciones y la pareja título/etiqueta se apilan en vez de
desbordar en un ancho angosto.

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
documento por una entidad (ver arriba), y CU-06 sin `descarga` masiva ni paquetes
(RF27).

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
    servicios.py               CU-01/CU-02: logica de negocio compartida entre la API JSON
                                y el portal (AD-11) -- registro e inicio/cierre de sesion
    primer_acceso.py           POST /api/v1/primer-acceso y /primer-acceso/reenviar: rutas
                                JSON, delgadas sobre primer_acceso_servicios.py
    primer_acceso_servicios.py logica compartida con el portal, incluido el modelo
                                SolicitudPrimerAcceso (con el validador de formato de
                                password) y el mensaje fijo de reenvio
    perfil.py                  GET/PATCH /api/v1/perfil: rutas JSON, delgadas sobre
                                perfil_servicios.py (CU-03 no incluido: ver interoperabilidad/)
    perfil_totp.py              POST/POST confirmar/DELETE segundo factor: rutas JSON,
                                delgadas sobre perfil_servicios.py
    perfil_servicios.py        logica compartida con el portal: datos+cuota del ciudadano
                                y enrolamiento/confirmacion/baja del TOTP
  notificaciones/
    correo.py                  envío de correo simulado (sin proveedor real integrado) + registro en `notificacion`
    router.py                  GET /api/v1/notificaciones y POST .../leida: rutas JSON,
                                delgadas sobre servicios.py
    servicios.py               logica compartida con la bandeja del portal (CU-17)
  documentos/
    router.py                  CU-05/06/07/08/10: rutas JSON, delgadas sobre servicios.py
    servicios.py               logica de negocio compartida entre la API JSON y el portal
                                (AD-11): carga, listado, descarga, eliminacion, autenticacion
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
    transferencias.py          router (/api, CU-16, formato del ecosistema, sin tocar) +
                                router_propio (/api/v1/perfil/traslado, CU-03: rutas JSON,
                                delgadas sobre traslado_servicios.py)
    traslado_servicios.py      logica de CU-03 compartida con el portal: solicitar_traslado,
                                listar_operadores_transferibles, estado_traslado
  mock/registraduria.py        Registraduría simulada
  portal/                      AD-11: todas las pantallas del ciudadano (Jinja2 + HTMX),
                                en la raiz del dominio -- no aparece en el esquema OpenAPI
    auth.py                    cookie de sesion del portal (HttpOnly + SameSite + Secure)
    router.py                  sesion, registro, carpeta, documento (detalle, descarga,
                                eliminar, autenticacion, sustituir CU-10, estado en vivo) +
                                router_legado (redirecciones permanentes desde /portal/...)
    router_notificaciones.py   bandeja de CU-17 + el contador de la navegacion
    router_perfil.py           perfil (datos y cuota) + segundo factor (TOTP)
    router_traslado.py         CU-03: elegir operador, confirmar, estado en vivo
    router_primer_acceso.py    primer acceso por token, y su reenvio
    templates/                 base.html (nav comun a toda pantalla autenticada),
                                sesion.html, registro.html, carpeta.html, documento.html,
                                sustituir.html, notificaciones.html, perfil.html, totp.html,
                                traslado.html, primer_acceso.html,
                                primer_acceso_reenviar.html, _macros.html (etiquetas de
                                procedencia/firma), _documento_estado.html y
                                _traslado_estado.html (fragmentos sondeados por HTMX)
    static/                    htmx.min.js (vendorizado) y estilos.css (mobile-first)
  admin/                        AD-12: consola de administracion de solo lectura
                                (RF32-RF37, CU-22), bajo /admin -- fuera del esquema
                                OpenAPI, sin ningun enlace desde el portal (si enlaza
                                de vuelta al portal desde su propia cabecera)
    auth.py                    cookie de sesion propia (colcarpeta_admin_sesion,
                                path=/admin) + JWT independiente del de identidad/token.py
    servicios.py               consultas de solo lectura de las 5 pantallas + login con
                                bloqueo por intentos (mismo patron que el ciudadano)
    router.py                  /admin/login, /admin/salir, y las 5 pantallas protegidas
    templates/                 base_admin.html (reusa estilos.css del portal) + una
                                plantilla por pantalla (resumen, ciudadanos,
                                transferencias, bandeja_salida, auditoria)
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
scripts/probar_portal.py  portal (AD-11), primera pasada, por las pantallas HTML: registro, login, subir, listar, ver, descargar, eliminar, salir
scripts/probar_portal_segunda_pasada.py  portal (AD-11), segunda pasada: notificaciones, perfil, segundo factor, sustituir (CU-10) y traslado (CU-03) por las pantallas HTML
scripts/probar_portal_primer_acceso.py  primer acceso por el portal con un token REAL de una transferencia (usar despues de probar_portal_segunda_pasada.py)
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
docker compose -f docker-compose.test.yml run --rm prueba-portal   # portal (AD-11), primera pasada
docker compose -f docker-compose.test.yml run --rm prueba-portal-segunda-pasada   # notificaciones, perfil, TOTP, sustituir, traslado
docker compose -f docker-compose.test.yml logs app-b | grep -A5 "correo simulado"   # token de primer acceso (tras el traslado de arriba)
docker compose -f docker-compose.test.yml run --rm prueba-portal-primer-acceso \
  --base-url=http://app-b:8000 --token=<el-extraido-arriba> --usuario=<cedula>
docker compose -f docker-compose.test.yml down -v             # -v: tambien borra postgres-a/b
```

`scripts/probar_envio_transferencia.py` registra un ciudadano de prueba en `app-a`
(cédula ficticia -- es seguro, nunca toca el MinTIC real), sube un documento, solicita
el traslado a `test-operador-b`, y verifica en las dos bases de datos (consulta directa
por SQL: este script es anterior a `GET /api/v1/perfil`, no se actualizó para usarlo) que
la transferencia quedó `CONFIRMADA`, el ciudadano llegó `ACTIVO` a `app-b` con su
documento, y `app-a` quedó `TRASLADADO`. Probado de punta a punta el 2026-09-22, varias
veces, de forma reproducible.

`scripts/probar_reconciliacion.py` inserta cuatro filas `transferencia` `ENVIADA` con
`enviada_en` retrocedido en la base (no espera `TRANSFER_CONFIRM_TIMEOUT` de verdad) y
corre `_reconciliar_transferencias` una sola vez, cubriendo (desde el 2026-09-23, tras
la simplificación de la reconciliación) sus dos desenlaces posibles -- ya no hay un
tercero ambiguo -- en sus cuatro variantes de entrada: A confirmada por el destino
exacto, B disponible y recuperada, C confirmada igual que A pero afiliada a un tercero
distinto del destino elegido, D recuperada igual que B pero afiliada a ColCarpeta mismo
(como si `unregisterCitizen` no hubiera surtido efecto en el envío original). No
depende de `app-a`/`app-b` como servidores vivos -- solo de `postgres-a` y
`mock-centralizador` -- para no competir con su propio outbox por las mismas filas.

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

`scripts/probar_portal.py` ejercita el portal del ciudadano (AD-11) por sus pantallas
HTML, contra `app-a` viva: confirma que `GET /` sin sesión lleva a iniciar sesión (en
vez del 404 que devolvía antes de moverse a la raíz), registra una cédula de prueba
por el formulario de `/registro`, espera a que quede `ACTIVO`, intenta iniciar sesión
con una contraseña incorrecta primero (confirma el mensaje en lenguaje claro), inicia
sesión de verdad por `/sesion` y confirma que la cookie de sesión queda fija en el
cliente HTTP (`httpx.AsyncClient`, que conserva cookies entre peticiones igual que un
navegador), sube un documento por el formulario de la carpeta y confirma que aparece
de inmediato con la etiqueta "Temporal" y su nota de procedencia, entra al detalle y
confirma "Sin firma digital" (el PDF de prueba no está firmado) y la sección de
autenticación ante GovCarpeta, descarga el documento y compara los bytes exactos
contra lo subido, lo elimina desde la carpeta y confirma el mensaje de éxito y que
desaparece del listado, confirma que ocho rutas viejas bajo `/portal/...` (raíz,
carpeta con y sin cadena de consulta, sesión, registro, detalle y descarga de un
documento, un estático) responden 308 con el `Location` correcto hacia su ruta nueva
sin `follow_redirects` (para poder inspeccionar el código y el encabezado en vez de
solo llegar al destino), y cierra sesión confirmando que `/carpeta` vuelve a exigir
inicio de sesión. Encontró la trampa de la cookie `Secure` sobre HTTP simple descrita
arriba (corregida con `PORTAL_COOKIE_SECURE=false` en el entorno de `app-a` de este
archivo) -- ese fue el único bug real que encontró en su primera corrida; el resto de
la prueba, incluida la extensión para el movimiento a la raíz, pasó sin ajustes
adicionales.

`scripts/probar_portal_segunda_pasada.py` cubre las seis pantallas de la segunda
pasada, contra `app-a` viva (con `app-b` y `mock-centralizador` como destino real del
traslado): registra por el portal con un correo personal real (para que el traslado
más adelante traiga `contactEmail`), inicia sesión (el login no exige `ACTIVO`, solo
credenciales válidas) y reintenta la carga de un documento hasta que el registro
termina de confirmarse (en vez de sondear la base directamente, a diferencia de otros
scripts de esta lista, porque este es deliberadamente HTTP-only), sustituye ese
documento (CU-10) y confirma que el original queda consultable como reemplazado, revisa
la bandeja de notificaciones y la marca como leída, confirma el fragmento del contador,
consulta y actualiza el perfil, habilita el segundo factor con el código simulado y lo
confirma, lo ve reflejado tanto en `/perfil/totp` como en la etiqueta de `/perfil`, y lo
deshabilita, prueba que el formulario de traslado rechaza la solicitud sin la casilla
de confirmación marcada, la envía de verdad a `test-operador-b`, espera activamente a
que se confirme, y confirma que el login después responde con el mensaje de carpeta ya
trasladada. Encontró dos bugs reales, ambos corregidos antes de darse por terminada la
tarea: la trampa de la cookie `Secure` en `app-b` (ver arriba, la misma ya conocida de
`app-a`) y el problema de `en_curso` en la pantalla de traslado (ver arriba, específico
de esta pantalla nueva) -- este segundo lo encontró la revisión de código, no la
ejecución de la prueba, que nunca llegó a fallar por esa ventana de tiempo tan corta.

`scripts/probar_portal_primer_acceso.py` consume, por el portal
(`GET`/`POST /primer-acceso`), el token real de primer acceso que
`probar_portal_segunda_pasada.py` deja en el log de `app-b` al completarse el traslado
-- mismo patrón de extracción manual que `scripts/probar_primer_acceso.py` (API JSON):
abre el enlace con el token en la URL y confirma que llega precargado en el campo,
establece la contraseña, confirma que reusar el mismo token ya falla con el mensaje en
lenguaje claro ("El enlace no es válido o ya venció", sin distinguir el motivo), e
inicia sesión por el portal con la contraseña nueva, confirmando que la cookie de
sesión queda fija. Encontró la misma trampa de la cookie `Secure` en `app-b` la primera
vez que corrió (antes de que `probar_portal_segunda_pasada.py` la hubiera dejado
corregida en el archivo); en la corrida final, con la corrección ya aplicada, pasó sin
ajustes.

Como regresión sobre las rutas JSON que ahora comparten la capa de servicios recién
extraída para la segunda pasada, se corrieron de nuevo (contra el mismo trío
`app-a`/`app-b`/`mock-centralizador`) `scripts/probar_carpeta_completa.py`,
`scripts/probar_envio_transferencia.py`, `scripts/probar_primer_acceso.py`,
`scripts/probar_reconciliacion.py` y `scripts/probar_regreso_antes_de_purga.py`: los
cinco siguen sin fallos, confirmando que extraer `perfil_servicios.py`,
`primer_acceso_servicios.py`, `notificaciones/servicios.py` y `traslado_servicios.py`
no cambió el comportamiento de ninguna ruta JSON existente.

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
