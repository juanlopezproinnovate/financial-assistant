"""
app/services/gemini_service.py
Motor NLP migrado a Groq — Llama 3.3 70b
Mismo nombre de archivo/clase para no cambiar imports en webhook.py
Límites Groq free: 14,400 RPD / 30 RPM
"""

import json
import re
import logging
import datetime
from groq import AsyncGroq
from app.config import settings

logger = logging.getLogger(__name__)

client = AsyncGroq(api_key=settings.GROQ_API_KEY)

MODELO_NLP = "llama-3.3-70b-versatile"

# ──────────────────────────────────────────────
#  SYSTEM PROMPT
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres Quri, el asistente de negocios por WhatsApp para comerciantes de ropa de Tacna, Perú.
Tu misión es ayudar a registrar ventas, gastos e inventario usando lenguaje natural.

PERSONALIDAD:
- Habla en español peruano natural, cálido y directo.
- Usa emojis con moderación (1-2 por mensaje).
- Sé conciso: máximo 3-4 líneas. Los comerciantes están ocupados.
- Tutéalo siempre.
- Si hay errores ortográficos o spanglish, entiéndelo igual.

CONTEXTO:
Tacna es zona de comercio transfronterizo. Comerciantes compran en Bolivia, Chile y Puno.
Manejan ropa de dama, caballero, niños, zapatos, accesorios.
Monedas: Soles (S/), dólares ($), bolivianos (Bs).

REGLA CRÍTICA DE FORMATO:
SIEMPRE responde SOLO con JSON válido. Sin texto antes ni después. Sin markdown.

Estructura OBLIGATORIA:
{
  "intent": "VENTA|GASTO|INVENTARIO|REPORTE|AYUDA|SALUDO|ELIMINAR_TRANSACCION|EDITAR_TRANSACCION|DESCONOCIDO",
  "datos": {},
  "respuesta": "texto para WhatsApp",
  "requiere_confirmacion": false,
  "siguiente_paso": ""
}

INTENTS:

ELIMINAR_TRANSACCION — frases: eliminar transaccion, borrar la ultima venta, me equivoque borra eso
datos: {}

EDITAR_TRANSACCION — frases: editar transaccion, cambiar monto de la venta, quiero modificar un gasto
datos: {}

VENTA — frases: vendí, vendiste, vendimos, salió una, me llevaron, acabo de vender
datos: {"producto": str, "cantidad": int, "precio_unitario": float, "total": float, "moneda": "PEN|USD|BOB", "fecha": "YYYY-MM-DD", "hora": "HH:MM:SS"}
Si falta precio o cantidad, pídelos en respuesta.

GASTO — frases: gasté, pagué, compré mercadería, flete, pasajes, alquiler
datos: {"concepto": str, "monto": float, "moneda": "PEN|USD|BOB", "categoria": "mercaderia|transporte|local|servicios|otros", "fecha": "YYYY-MM-DD", "hora": "HH:MM:SS"}

FECHAS Y HORAS:
Si el usuario especifica fecha (ayer, hace 3 días, etc.), calcúlala basándote en la "Fecha de hoy" provista.
Si no especifica fecha, usa la "Fecha de hoy".
Si no especifica hora, pon "12:00:00" por defecto.
Asegúrate de SIEMPRE devolver "fecha" y "hora" en VENTA y GASTO.

INVENTARIO — frases: tengo, me quedan, llegaron, entró mercadería
datos: {"producto": str, "cantidad": int}

REPORTE — frases: cómo voy, cuánto vendí, resumen, total, mis ventas
datos: {"periodo": "hoy|ayer|semana|mes"}

SALUDO — hola, buenas, buenos días, qué tal
Responde con energía y pregunta en qué ayudas.

MONEDAS:
S/, soles → "PEN"
$, dólares → "USD"
Bs, bolivianos → "BOB"
Sin moneda → asumir "PEN"

EJEMPLOS:
"vendí 3 polos a 25 soles" → VENTA, producto:polo, cantidad:3, precio_unitario:25, total:75, moneda:PEN
"gasté 200 en pasajes a Bolivia" → GASTO, concepto:pasajes Bolivia, monto:200, moneda:PEN, categoria:transporte
"cuánto vendí hoy" → REPORTE, periodo:hoy
"borra esa venta" → ELIMINAR_TRANSACCION
"quiero modificar el monto" → EDITAR_TRANSACCION
"hola" → SALUDO
"""

ONBOARDING_PROMPTS = {
    1: """Responde SOLO con JSON. Usuario nuevo en Boti, paso 1.
La "respuesta" debe ser bienvenida cálida que: salude, explique en 2 líneas qué hace Boti (ventas/gastos/inventario por WhatsApp), y pida el nombre del negocio.
{"intent":"ONBOARDING","datos":{"paso":1},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    2: """Responde SOLO con JSON. Paso 2 del onboarding.
La "respuesta" pregunta qué tipo de ropa vende (dama, caballero, niños, todo).
{"intent":"ONBOARDING","datos":{"paso":2},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    3: """Responde SOLO con JSON. Paso 3 del onboarding.
La "respuesta" pregunta a qué hora cierra la tienda para enviar resumen diario.
{"intent":"ONBOARDING","datos":{"paso":3},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    4: """Responde SOLO con JSON. Paso 4 del onboarding, todo configurado.
La "respuesta" celebra que está listo y muestra ejemplo: "Vendí 2 blusas a S/35 cada una".
{"intent":"ONBOARDING","datos":{"paso":4},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",
}


class GeminiService:
    """Mismo nombre de clase para no tocar imports en webhook.py."""

    def _parsear(self, raw: str) -> dict:
        raw = raw.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        # Extraer primer objeto JSON si hay texto extra
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group()
        return json.loads(raw)

    async def procesar_mensaje(
        self,
        mensaje: str,
        historial: list[dict] = None,
        contexto_negocio: dict = None,
    ) -> dict:
        contexto_negocio = contexto_negocio or {}
        mensaje_final = f"[Fecha de hoy: {datetime.date.today()}] {mensaje}"
        if contexto_negocio.get("nombre"):
            mensaje_final = f"[Negocio: {contexto_negocio['nombre']}, ropa: {contexto_negocio.get('tipo_ropa','')} | Fecha de hoy: {datetime.date.today()}] {mensaje}"

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": mensaje_final},
                ],
                temperature=0.2,
                max_tokens=512,
            )
            raw = response.choices[0].message.content
            return self._parsear(raw)

        except json.JSONDecodeError:
            logger.warning(f"[Groq] JSON inválido: {raw[:200]}")
            return {
                "intent": "DESCONOCIDO",
                "datos": {},
                "respuesta": "No entendí bien. ¿Puedes decirme qué vendiste o en qué te ayudo? 😊",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }
        except Exception as e:
            logger.error(f"[Groq] Error procesando mensaje: {e}")
            return {
                "intent": "ERROR",
                "datos": {},
                "respuesta": "Ups, tuve un problema técnico. Intenta de nuevo 🙏",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    async def procesar_onboarding(self, paso: int, mensaje_usuario: str = "") -> dict:
        prompt = ONBOARDING_PROMPTS.get(paso, ONBOARDING_PROMPTS[1])
        if mensaje_usuario:
            prompt += f"\nEl usuario escribió: '{mensaje_usuario}'"

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres Boti, asistente de negocios por WhatsApp para comerciantes de ropa en Tacna, Perú. Habla en español peruano cálido y natural."},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.3,
                max_tokens=256,
            )
            raw = response.choices[0].message.content
            return self._parsear(raw)

        except Exception as e:
            logger.error(f"[Groq] Error onboarding paso {paso}: {e}")
            fallbacks = {
                1: "¡Hola! Soy Boti 👋 Te ayudo a controlar ventas y gastos de tu negocio por WhatsApp. Para empezar, ¿cómo se llama tu negocio?",
                2: "¿Qué tipo de ropa vendes principalmente? (dama, caballero, niños, todo)",
                3: "¿A qué hora cierras tu tienda? Te mando el resumen del día a esa hora 📊",
                4: "¡Todo listo! 🎉 Prueba escribiendo: 'Vendí 2 polos a S/25 cada uno'",
            }
            return {
                "intent": "ONBOARDING",
                "datos": {"paso": paso},
                "respuesta": fallbacks.get(paso, "¡Hola! ¿Cómo se llama tu negocio?"),
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    async def generar_resumen_reporte(self, datos: dict) -> str:
        prompt = f"""Eres Boti. Genera un resumen de reporte para WhatsApp con estos datos:
{json.dumps(datos, ensure_ascii=False)}

Reglas:
- Máximo 5 líneas
- Emojis (💰📦📊✅)
- Mostrar ventas, gastos y ganancia neta en soles
- Español peruano natural
- SIN asteriscos ni markdown
- Terminar con frase motivadora corta
Solo responde el texto, sin JSON."""

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=200,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            tv = datos.get("total_ventas", 0)
            tg = datos.get("total_gastos", 0)
            return (
                f"📊 Reporte de {datos.get('periodo', 'hoy')}:\n"
                f"💰 Ventas: S/{tv:.2f}\n"
                f"📦 Gastos: S/{tg:.2f}\n"
                f"✅ Ganancia: S/{tv - tg:.2f}"
            )

    async def interpretar_edicion(self, transaccion: dict, mensaje: str) -> dict:
        prompt = f"""Eres Boti. Un usuario quiere editar una transacción existente.
Transacción actual:
{json.dumps(transaccion, ensure_ascii=False)}

El usuario ha dicho: "{mensaje}"

Devuelve SOLO un JSON con los campos de la transacción que deben actualizarse. 
Posibles campos: "descripcion", "monto", "moneda", "tipo".
Ejemplo: si dice "cambia el monto a 50", devuelve {{"monto": 50}}.
Si dice "era en dolares", devuelve {{"moneda": "USD"}}.
Si dice "la descripcion era polos", devuelve {{"descripcion": "polos"}}.
Si no entiendes qué cambiar, devuelve un JSON vacío {{}}.
No incluyas texto adicional ni markdown."""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=150,
            )
            raw = response.choices[0].message.content.strip()
            return self._parsear(raw)
        except Exception as e:
            logger.error(f"[Groq] Error interpretando edición: {e}")
            return {}


gemini_service = GeminiService()