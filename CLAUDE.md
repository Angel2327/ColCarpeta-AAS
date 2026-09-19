# ColCarpeta — Operador de Carpeta Ciudadana

Proyecto del curso Arquitecturas Avanzadas de Software. Implementa la solución de **un
operador** de Carpeta Ciudadana. El MinTIC es un actor externo: opera el centralizador,
publica los servicios de interoperabilidad y además opera el operador GovCarpeta.

**La especificación completa está en `docs/especificacion.md`.** Antes de implementar
cualquier cosa, lee la sección correspondiente ahí. Este archivo solo resume lo que hay
que tener presente todo el tiempo.

## Identidad del operador

| Dato | Valor |
| --- | --- |
| Nombre | ColCarpeta |
| `operatorId` ante el MinTIC | `6aaeb415b765590002607402` |
| API del centralizador | `https://govcarpeta-apis-4905ff3c005b.herokuapp.com` |

## Stack

Python 3.12 · FastAPI sobre Uvicorn · SQLAlchemy 2 async · Alembic · httpx ·
PostgreSQL y almacenamiento S3 en Supabase · despliegue en Railway.

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
- **Toda operación relevante deja registro en `auditoria`**, que es de solo inserción.
- **Ningún secreto en el repositorio.** Solo `.env.example` con los nombres.

## Comandos

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload                 # desarrollo
alembic revision --autogenerate -m "mensaje"  # nueva migración
alembic upgrade head                          # aplicar migraciones
python scripts/probar_govcarpeta.py           # prueba de humo contra la API real
```

## Convenciones

- Nombres de dominio en español (`ciudadano`, `documento`, `outbox`), código en inglés
  donde sea idiomático de la librería.
- Errores de la API propia siempre con el sobre `{"error": {...}}` de `app/errors.py`.
  Los endpoints de transferencia entre operadores NO usan ese formato: responden con lo
  que define el acuerdo del ecosistema, para no romper a los demás operadores.
- Un módulo por componente lógico bajo `app/`. Las fronteras entre módulos se respetan
  aunque hoy compartan proceso: la arquitectura objetivo separa `interoperabilidad`.
