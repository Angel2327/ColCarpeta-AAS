"""agregar extension unaccent para busquedas sin acentos

Revision ID: 6c5a3c89d46e
Revises: 567f21d2ce7a
Create Date: 2026-09-24 22:12:09.083118

Habilita `unaccent` para que los filtros de texto humanos (tipo, entidad emisora,
titulo de un documento, nombre de un ciudadano en la consola) encuentren "Muñoz"
buscando "munoz" y "Académico" buscando "academico", sin distinguir acentos ademas de
no distinguir mayusculas (que ya no distinguian desde antes). No toca `cedula` (son
digitos, un acento no aplica) ni `accion` de auditoria (vocabulario interno del
codigo, nunca texto que un ciudadano escriba).

Disponibilidad y privilegios verificados de antemano contra la base real, con
consultas de solo lectura -- ningun DDL contra produccion, ni siquiera dentro de una
transaccion revertida (CLAUDE.md, "Prohibido": una version anterior de esta misma
investigacion SI llego a ejecutar un `CREATE EXTENSION` de prueba contra la base real
dentro de un `ROLLBACK`; quedo corregido para no volver a hacerlo):

    SELECT name, default_version, installed_version
    FROM pg_available_extensions WHERE name = 'unaccent';
    -- name='unaccent', default_version='1.1', installed_version=NULL
    -- (el binario esta disponible en el servidor; no esta instalada)

    SELECT rolname, rolsuper, rolcreatedb FROM pg_roles
    WHERE pg_has_role(current_user, oid, 'member');
    -- el rol "postgres" (con el que se conecta la aplicacion) es miembro de
    -- "pg_database_owner" -- es DUEÑO de la base "postgres" -- aunque rolsuper=false.

Postgres 17.6 (Supabase). Desde PostgreSQL 13, el DUEÑO de una base de datos puede
ejecutar `CREATE EXTENSION` sobre cualquier extension marcada "trusted" en su archivo
de control, sin necesitar superusuario -- `unaccent` es una de las extensiones
"trusted" del contrib estandar de Postgres (lo es desde que existe esa categoria). La
membresia a `pg_database_owner` confirmada arriba, combinada con ese hecho conocido
del propio Postgres (no algo que dependa de esta instalacion), es la base de la
conclusion "deberia poder instalarse sin pasos manuales" -- pero el `CREATE EXTENSION`
en si NO se ejecuto contra la base real para comprobarlo (esa comprobacion, deliberada
y explicitamente, se hizo solo contra `postgres-a`, la base desechable de
`docker-compose.test.yml`, Postgres 16 -- ver el reporte de la tarea para los tres
escenarios probados ahi: base limpia, base con datos, y el downgrade). Si al aplicar
esta migracion contra la base real algo saliera distinto a lo esperado (por ejemplo,
si Supabase restringe `unaccent` de su lista de extensiones "trusted" pese a que el
binario este disponible), el error de Postgres al respecto seria explicito y esta
migracion simplemente no aplicaria -- no hay manera de que falle en silencio.
"""
from alembic import op
import sqlalchemy as sa


revision = '6c5a3c89d46e'
down_revision = '567f21d2ce7a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # DROP EXTENSION falla si algo mas de la base depende de unaccent (un indice o una
    # vista, por ejemplo) -- nada en este proyecto crea eso hoy, pero si alguien lo
    # agregara despues, este downgrade fallaria en vez de dejar la base en un estado
    # a medias, que es el comportamiento correcto.
    op.execute("DROP EXTENSION IF EXISTS unaccent")
