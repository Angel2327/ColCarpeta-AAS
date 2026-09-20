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
migraciones aplicadas · `app/interoperabilidad/govcarpeta.py` (cliente completo del
centralizador) · `app/interoperabilidad/outbox.py` (proceso de bandeja de salida) ·
`app/mock/registraduria.py` · `app/identidad/` (registro, correo, seguridad).

**CU-01, registro del ciudadano**: implementado, con los flujos A1, A2 y E1 a E6.
**CU-02, inicio de sesión**: implementado, con A1, A2 y E1 a E4.

### Pendiente — 2 de los 4 flujos obligatorios de la entrega

1. **CU-05** carga de documentos
2. **CU-11** autenticación ante GovCarpeta

Fuera de alcance por ahora: transferencia entre operadores (diseñada, sin implementar),
notificaciones más allá del correo de registro, consola de administración.

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
    outbox.py                  proceso de bandeja de salida
  mock/registraduria.py        Registraduría simulada
alembic/versions/              migraciones
docs/especificacion.md         la especificación completa
scripts/probar_govcarpeta.py   prueba de humo contra la API real
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
`validateCitizen` devuelve 200 y sirve para probar E1 de CU-01.

## Convenciones

- Nombres de dominio en español (`ciudadano`, `documento`, `outbox`), código en inglés
  donde sea idiomático de la librería.
- Errores de la API propia siempre con el sobre `{"error": {...}}` de `app/errors.py`.
  Los endpoints de transferencia entre operadores NO usan ese formato: responden con lo
  que define el acuerdo del ecosistema, para no romper a los demás operadores.
- Un módulo por componente lógico bajo `app/`. Las fronteras entre módulos se respetan
  aunque hoy compartan proceso: la arquitectura objetivo separa `interoperabilidad`.
- Cuando termines una parte, actualiza la sección **Estado actual** de este archivo.
