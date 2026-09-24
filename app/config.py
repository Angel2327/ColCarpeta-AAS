from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # base de datos
    database_url: str = "postgresql://postgres:postgres@localhost:5432/postgres"

    # almacenamiento de objetos
    s3_endpoint: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "documentos"

    # operador
    operator_id: str = ""
    operator_name: str = "ColCarpeta"
    govcarpeta_url: str = "https://govcarpeta-apis-4905ff3c005b.herokuapp.com"
    public_base_url: str = ""
    dominio_carpeta: str = "carpetacolombia.co"

    # sesion
    # JWT RS256 ("Acceso del ciudadano"). El PEM puede llegar con saltos de linea reales
    # (.env local, donde python-dotenv ya los desescapa dentro de comillas) o con "\n"
    # literal (variables de entorno de Railway, que no admiten multilinea) -- el
    # validador de abajo normaliza cualquiera de las dos formas.
    jwt_llave_privada: str = ""
    jwt_llave_publica: str = ""
    jwt_minutos: int = 30
    intentos_login_maximos: int = 5
    intentos_login_ventana_minutos: int = 15
    bloqueo_login_minutos: int = 15

    # segundo factor
    totp_modo: str = "simulado"
    totp_codigo_simulado: str = "000000"
    totp_pendiente_minutos: int = 15
    totp_periodo_segundos: int = 30
    totp_tolerancia_periodos: int = 1

    # limites
    cuota_ciudadano_bytes: int = 209_715_200
    tamano_maximo_archivo_bytes: int = 20_971_520
    presigned_url_ttl_auth: int = 900
    presigned_url_ttl_transfer: int = 86_400
    # No tiene variable propia en "Enlaces firmados" (esa tabla solo lista centralizador
    # y operador destino): se agrega para /api/v1/documentos/{id}/descarga, corto porque
    # el portal redirige de inmediato y el enlace no se reutiliza.
    presigned_url_ttl_descarga: int = 300
    transfer_confirm_timeout: int = 14_400
    purge_delay_days: int = 30
    outbox_intervalo_segundos: int = 10
    # Umbral para revivir filas de outbox colgadas en EN_PROCESO (el proceso que las
    # tomo murio, se colgo, o hubo un redespliegue a mitad de ejecucion). 15 minutos da
    # margen de sobra frente al peor caso legitimo (una transferencia con hasta 200
    # documentos, cada uno con su propio limite duro de red de ~1 minuto): un umbral
    # mas corto arriesga revivir una entrada que en realidad sigue viva y en curso.
    outbox_en_proceso_maximo_segundos: int = 900
    # "Parametros y limites" > Documentos: limites de una transferencia entrante.
    transferencia_documentos_maximo: int = 200
    transferencia_tamano_maximo_bytes: int = 500 * 1024 * 1024
    # "Directorio de operadores": refresco periodico de operador_cache via getOperators.
    # El refresco "a demanda antes de cada envio" (CU-03) no usa este intervalo -- ocurre
    # siempre, sin importar cuanto falte para el proximo refresco periodico.
    directorio_operadores_refresco_segundos: int = 900
    # CU-03: exigir que transferAPIURL del destino sea https antes de enviarle algo.
    # SIEMPRE True salvo en docker-compose.test.yml, donde dos instancias propias se
    # hablan por HTTP simple dentro de una red Docker aislada (sin TLS entre
    # contenedores) -- nunca se debe apagar en produccion ni en un .env real.
    transferencia_exigir_https: bool = True
    # Operadores del directorio que decidimos no ofrecer en el desplegable de traslado
    # (ver docs/especificacion.md, "Directorio de operadores"): una decision de
    # operacion propia, no parte del acuerdo con el MinTIC -- el directorio sigue
    # siendo la fuente de verdad, esto solo filtra que le mostramos al ciudadano.
    # _id separados por coma, NUNCA operatorName (el directorio tiene nombres
    # duplicados, CLAUDE.md "trampa 5"). Variable de entorno, no una lista escrita en
    # el codigo, para poder sacar a alguien de la lista sin redesplegar el dia que
    # arregle su operador.
    operadores_excluidos: str = ""
    # Vigencia del token de un solo uso con el que un ciudadano recibido por
    # transferencia establece su contrasena inicial (ver app.identidad.token_acceso).
    primer_acceso_token_ttl_horas: int = 24
    # Reenvio del token de primer acceso: limite de solicitudes por cedula y por origen
    # dentro de la ventana, igual patron que el bloqueo de inicio de sesion (derivado de
    # auditoria, sin tabla propia).
    primer_acceso_reenvio_maximo: int = 3
    primer_acceso_reenvio_ventana_minutos: int = 15

    registraduria_api_key: str = "clave-interna-de-desarrollo"

    # portal del ciudadano (AD-11): la sesion viaja en una cookie HttpOnly + SameSite,
    # nunca en un token que JavaScript pueda leer. SIEMPRE True salvo en
    # docker-compose.test.yml, donde el portal se prueba por HTTP simple dentro de una
    # red Docker aislada (un navegador descarta una cookie "Secure" sobre HTTP) -- nunca
    # se debe apagar en produccion ni en un .env real, mismo patron que
    # transferencia_exigir_https.
    portal_cookie_secure: bool = True
    # Cada cuanto sondea sola, sin recargar, la insignia de notificaciones de la
    # navegacion (visible en cualquier pantalla autenticada). 10 minutos por defecto:
    # nadie nota la diferencia frente a un intervalo mas corto, porque la insignia ya
    # se repinta con datos frescos en cada cambio de pantalla (load); el sondeo
    # periodico solo cubre a alguien que se queda quieto en la misma pantalla.
    notificaciones_contador_intervalo_segundos: int = 600

    # consola de administracion (RF32-RF37, CU-19): sin modelo de usuarios, una sola
    # credencial en variable de entorno. Vacio por defecto = consola inutilizable hasta
    # configurarla explicitamente (nunca un "funciona" falso con un hash vacio, mismo
    # patron que operadores_excluidos). El hash se genera con
    # app.identidad.seguridad.hash_password y se verifica con verificar_password, la
    # misma funcion que ya usa el ciudadano -- nunca se compara la clave en claro.
    admin_password_hash: str = ""

    @field_validator("jwt_llave_privada", "jwt_llave_publica")
    @classmethod
    def _normalizar_pem(cls, valor: str) -> str:
        """Acepta el PEM con saltos de linea reales o con "\\n" escapado, sea cual sea
        el origen (archivo .env vs. variable de entorno de la plataforma)."""
        return valor.replace("\\n", "\n") if "\\n" in valor else valor

    @property
    def operadores_excluidos_ids(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.operadores_excluidos.split(",") if o.strip())

    @property
    def sqlalchemy_url(self) -> str:
        """Supabase entrega la URL en formato psycopg; SQLAlchemy async necesita asyncpg."""
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@lru_cache
def get_config() -> Config:
    return Config()
