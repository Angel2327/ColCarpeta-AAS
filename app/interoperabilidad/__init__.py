"""Interoperabilidad: unico paquete que habla con el centralizador del MinTIC y con otros
operadores (CLAUDE.md). El resto de la aplicacion consume sus servicios a traves de lo
que se expone aqui; nadie mas importa `app.interoperabilidad.govcarpeta` directamente.
"""

from app.interoperabilidad.govcarpeta import CentralizadorNoDisponible, GovCarpeta, Operador, ResultadoValidacion

__all__ = [
    "CentralizadorNoDisponible",
    "GovCarpeta",
    "Operador",
    "ResultadoValidacion",
    "validar_ciudadano",
]


async def validar_ciudadano(cedula: int) -> ResultadoValidacion:
    """GET /apis/validateCitizen/{id}. Unica llamada al centralizador que es sincrona."""
    gov = GovCarpeta()
    try:
        return await gov.validar_ciudadano(cedula)
    finally:
        await gov.cerrar()
