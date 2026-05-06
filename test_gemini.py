"""
test_gemini.py  (va en la raíz del proyecto, junto a main.py)
Prueba el motor NLP localmente SIN WhatsApp ni BD.

Ejecutar desde la raíz:
    python test_gemini.py

Requiere GEMINI_API_KEY en .env
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Cargar .env antes de importar el servicio
from dotenv import load_dotenv
load_dotenv()

from app.services.gemini_service import gemini_service

VERDE  = "\033[92m"
ROJO   = "\033[91m"
RESET  = "\033[0m"
CYAN   = "\033[96m"

CASOS = [
    ("Saludo simple",            "hola buenas tardes",                         {}),
    ("Venta clara",              "vendí 3 polos a 25 soles cada uno",          {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Venta monto total",        "acabo de vender una casaca en 80 soles",     {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Venta en dólares",         "vendí 2 jeans a $15 cada uno",               {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Gasto transporte",         "gasté 200 soles en pasajes a Bolivia",       {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Gasto mercadería",         "compré mercadería por 800 soles en Desaguadero", {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Inventario",               "me quedan 15 jeans talla 32",                {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Reporte del día",          "cuánto vendí hoy",                           {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Spanglish / errores",      "vendí unas blusa a 35 cada una, 4 unidades", {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
    ("Mensaje ambiguo",          "ayer fue un buen día",                       {"nombre": "Boutique Ana", "tipo_ropa": "dama"}),
]

INTENTS_VALIDOS = {"VENTA", "GASTO", "INVENTARIO", "REPORTE", "SALUDO", "AYUDA", "RECORDATORIO", "DESCONOCIDO"}


async def test_nlp():
    print(f"\n{CYAN}{'='*60}")
    print("  TEST NLP — BOTI con Gemini 2.5 Flash")
    print(f"{'='*60}{RESET}\n")

    ok = fail = 0

    for desc, mensaje, contexto in CASOS:
        print(f"📝 {desc}")
        print(f"   Usuario: '{mensaje}'")

        result = await gemini_service.procesar_mensaje(mensaje=mensaje, contexto_negocio=contexto)

        intent   = result.get("intent", "?")
        datos    = result.get("datos", {})
        respuesta = result.get("respuesta", "")

        if intent not in ("ERROR",) and intent in INTENTS_VALIDOS:
            print(f"   {VERDE}✅ Intent: {intent}{RESET}")
            ok += 1
        else:
            print(f"   {ROJO}❌ Intent: {intent}{RESET}")
            fail += 1

        if datos:
            print(f"   📦 Datos: {datos}")
        print(f"   💬 Boti: {respuesta[:120]}")
        print()

    print(f"{CYAN}{'='*60}")
    print(f"  RESULTADO: {ok} OK / {fail} FALLIDOS de {len(CASOS)}")
    print(f"{'='*60}{RESET}\n")


async def test_onboarding():
    print(f"{CYAN}{'='*60}")
    print("  TEST ONBOARDING — 4 pasos")
    print(f"{'='*60}{RESET}\n")

    msgs = {1: "", 2: "Boutique Rosita", 3: "ropa de dama", 4: "8pm"}

    for paso in [1, 2, 3, 4]:
        result = await gemini_service.procesar_onboarding(paso=paso, mensaje_usuario=msgs[paso])
        respuesta = result.get("respuesta", "?")
        print(f"Paso {paso}: {respuesta[:150]}")
        print()


if __name__ == "__main__":
    asyncio.run(test_nlp())
    asyncio.run(test_onboarding())