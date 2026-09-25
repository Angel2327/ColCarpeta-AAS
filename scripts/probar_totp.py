"""Prueba del segundo factor (TOTP) en aislamiento -- sin Docker, sin base de datos,
sin red: genera un secreto con la misma libreria que usa la aplicacion (pyotp),
calcula codigos con ella, y los valida contra el verificador real de
`app.identidad.totp.verificar_codigo`, en modo TOTP_MODO=real (el modo que de verdad
usa pyotp; el modo "simulado" solo compara contra un codigo fijo y no necesita esta
prueba). Cubre exactamente los puntos que un telefono real no deja probar a mano:
el paso actual, la tolerancia de un periodo hacia atras/adelante, el rechazo de un
paso ya usado (anti-repeticion), un codigo con el secreto equivocado, y que el modo
"simulado" siga aceptando unicamente el codigo fijo configurado.

Uso:
    python scripts/probar_totp.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp  # noqa: E402

from app.config import Config  # noqa: E402
from app.identidad.totp import generar_secreto, verificar_codigo  # noqa: E402
from app.models import Ciudadano  # noqa: E402


def _ciudadano(secreto: str | None, ultimo_paso: int | None = None) -> Ciudadano:
    """Instancia en memoria, nunca persistida -- verificar_codigo solo lee/escribe
    estos dos atributos, no hace falta una sesion de base de datos para probarlo. El
    constructor por defecto de un modelo de SQLAlchemy acepta cualquier subconjunto de
    columnas mapeadas como argumentos con nombre."""
    return Ciudadano(totp_secret=secreto, totp_ultimo_paso=ultimo_paso)


def main() -> None:
    fallos: list[str] = []
    cfg_real = Config(totp_modo="real", totp_periodo_segundos=30, totp_tolerancia_periodos=1)

    print("1. Generando un secreto con la misma funcion que usa el enrolamiento...")
    secreto = generar_secreto()
    print(f"   secreto={secreto}")
    totp = pyotp.TOTP(secreto)  # defecto RFC 6238: SHA1, 6 digitos, periodo 30s

    print("2. Codigo del paso actual, calculado por pyotp -> debe validar...")
    codigo_actual = totp.now()
    ciudadano = _ciudadano(secreto)
    ok = verificar_codigo(ciudadano, codigo_actual, cfg_real)
    print(f"   codigo={codigo_actual} -> aceptado={ok}, totp_ultimo_paso quedo en {ciudadano.totp_ultimo_paso}")
    if not ok:
        fallos.append("el codigo del paso actual, generado con pyotp, deberia validar y no lo hizo")

    print("3. El MISMO codigo otra vez (mismo paso) -> debe RECHAZARSE (anti-repeticion)...")
    ok_repetido = verificar_codigo(ciudadano, codigo_actual, cfg_real)
    print(f"   codigo={codigo_actual} -> aceptado={ok_repetido}")
    if ok_repetido:
        fallos.append("el mismo codigo, para el mismo paso, se acepto dos veces (deberia rechazarse)")

    print("4. Codigo de UN PERIODO ATRAS (30s antes) -> debe validar (tolerancia=1)...")
    paso_actual = int(time.time() // cfg_real.totp_periodo_segundos)
    codigo_anterior = totp.at((paso_actual - 1) * cfg_real.totp_periodo_segundos)
    ciudadano2 = _ciudadano(secreto)
    ok_anterior = verificar_codigo(ciudadano2, codigo_anterior, cfg_real)
    print(f"   codigo={codigo_anterior} (paso {paso_actual - 1}) -> aceptado={ok_anterior}")
    if not ok_anterior:
        fallos.append("un codigo de un periodo atras deberia aceptarse dentro de la tolerancia y no se acepto")

    print("5. Codigo de DOS PERIODOS ATRAS (60s antes) -> debe RECHAZARSE (fuera de tolerancia=1)...")
    codigo_lejano = totp.at((paso_actual - 2) * cfg_real.totp_periodo_segundos)
    ciudadano3 = _ciudadano(secreto)
    ok_lejano = verificar_codigo(ciudadano3, codigo_lejano, cfg_real)
    print(f"   codigo={codigo_lejano} (paso {paso_actual - 2}) -> aceptado={ok_lejano}")
    if ok_lejano:
        fallos.append("un codigo de dos periodos atras esta fuera de la tolerancia configurada y se acepto igual")

    print("6. Codigo calculado con OTRO secreto -> debe RECHAZARSE...")
    otro_secreto = generar_secreto()
    codigo_ajeno = pyotp.TOTP(otro_secreto).now()
    ciudadano4 = _ciudadano(secreto)
    ok_ajeno = verificar_codigo(ciudadano4, codigo_ajeno, cfg_real)
    print(f"   codigo={codigo_ajeno} (de otro secreto) -> aceptado={ok_ajeno}")
    if ok_ajeno:
        fallos.append("un codigo calculado con un secreto distinto se acepto (no deberia)")

    print("7. Sin enrolamiento (totp_secret=None) -> cualquier codigo se RECHAZA...")
    ciudadano5 = _ciudadano(None)
    ok_sin_secreto = verificar_codigo(ciudadano5, codigo_actual, cfg_real)
    print(f"   -> aceptado={ok_sin_secreto}")
    if ok_sin_secreto:
        fallos.append("sin totp_secret, verificar_codigo no deberia aceptar ningun codigo")

    print("8. Modo TOTP_MODO=simulado -- solo el codigo fijo configurado debe validar...")
    cfg_simulado = Config(totp_modo="simulado", totp_codigo_simulado="000000")
    ciudadano6 = _ciudadano(secreto)  # el secreto real no importa en este modo
    ok_simulado_correcto = verificar_codigo(ciudadano6, "000000", cfg_simulado)
    ok_simulado_real = verificar_codigo(ciudadano6, codigo_actual, cfg_simulado)
    print(f"   codigo fijo '000000' -> aceptado={ok_simulado_correcto}")
    print(f"   codigo REAL de pyotp ({codigo_actual}) contra modo simulado -> aceptado={ok_simulado_real}")
    if not ok_simulado_correcto:
        fallos.append("en modo simulado, el codigo fijo configurado deberia aceptarse y no se acepto")
    if ok_simulado_real:
        fallos.append("en modo simulado, un codigo REAL de pyotp se acepto (no deberia: solo el codigo fijo vale)")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(" -", f)
        sys.exit(1)
    print("Prueba de TOTP (generacion y verificacion con pyotp, en aislamiento) sin fallos.")


if __name__ == "__main__":
    main()
