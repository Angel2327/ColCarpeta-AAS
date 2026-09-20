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
    transfer_confirm_timeout: int = 14_400
    purge_delay_days: int = 30
    outbox_intervalo_segundos: int = 10

    registraduria_api_key: str = "clave-interna-de-desarrollo"

    @field_validator("jwt_llave_privada", "jwt_llave_publica")
    @classmethod
    def _normalizar_pem(cls, valor: str) -> str:
        """Acepta el PEM con saltos de linea reales o con "\\n" escapado, sea cual sea
        el origen (archivo .env vs. variable de entorno de la plataforma)."""
        return valor.replace("\\n", "\n") if "\\n" in valor else valor

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
