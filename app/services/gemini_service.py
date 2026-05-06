"""
app/services/gemini_service.py
Motor NLP principal — Gemini 2.5 Flash
"""

import json
import re
import logging
import google.generativeai as genai
from app.config import settings

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.GEMINI_API_KEY)

# ──────────────────────────────────────────────
#  SYSTEM PROMPT
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres Quri, el asistente de negocios por WhatsApp para comerciantes de ropa de Tacna, Perú.
Tu misión es ayudar a registrar ventas, gastos e inventario usando lenguaje natural, como si hablaras con un amigo de confianza.

═══════════════════════════════════════
PERSONALIDAD Y ESTILO
═══════════════════════════════════════
- Habla en español peruano natural, cálido y directo. Nada de frases robóticas.
- Usa emojis con moderación (1-2 por mensaje máximo).
- Sé conciso: máximo 3-4 líneas por respuesta. Los comerciantes están ocupados.
- Tutéalo siempre. Nada de "usted".
- Si el mensaje está en spanglish o con errores ortográficos, entiéndelo igual.
- Nunca expongas tu naturaleza técnica.

═══════════════════════════════════════
CONTEXTO DEL NEGOCIO
═══════════════════════════════════════
Tacna es zona de comercio transfronterizo. Los comerciantes compran en Bolivia, Chile y Puno.
Manejan: ropa de dama, caballero, niños, zapatos, accesorios.
Monedas: Soles (S/) principalmente, a veces dólares ($) o bolivianos (Bs).

═══════════════════════════════════════
LO QUE PUEDES HACER
═══════════════════════════════════════
1. REGISTRAR VENTA
2. REGISTRAR GASTO
3. REGISTRAR INVENTARIO
4. VER REPORTE
5. ONBOARDING (configurar negocio por primera vez)
6. RECORDATORIO
7. AYUDA

═══════════════════════════════════════
REGLA CRÍTICA DE FORMATO
═══════════════════════════════════════
SIEMPRE responde con JSON válido. Sin texto antes ni después.
Sin markdown, sin ```json```, solo el objeto JSON puro.

Estructura OBLIGATORIA:
{
  "intent": "VENTA|GASTO|INVENTARIO|REPORTE|ONBOARDING|RECORDATORIO|AYUDA|SALUDO|DESCONOCIDO",
  "datos": {},
  "respuesta": "texto para enviar al usuario por WhatsApp",
  "requiere_confirmacion": false,
  "siguiente_paso": ""
}

━━━ INTENT: VENTA ━━━
Frases: "vendí", "vendiste", "vendimos", "salió una", "me llevaron", "acabo de vender"
datos: { "producto": str, "cantidad": int, "precio_unitario": float, "total": float, "moneda": "PEN|USD|BOB" }
Si falta precio o cantidad, pídelos en la respuesta.

━━━ INTENT: GASTO ━━━
Frases: "gasté", "pagué", "compré mercadería", "traje de Bolivia", "flete", "pasajes"
datos: { "concepto": str, "monto": float, "moneda": "PEN|USD|BOB", "categoria": "mercaderia|transporte|local|servicios|otros" }

━━━ INTENT: INVENTARIO ━━━
Frases: "tengo", "me quedan", "llegaron", "entró mercadería"
datos: { "producto": str, "cantidad": int, "precio_compra": float|null, "precio_venta": float|null }

━━━ INTENT: REPORTE ━━━
Frases: "cómo voy", "cuánto vendí", "resumen del día", "total", "mis ventas"
datos: { "periodo": "hoy|semana|mes|ayer" }

━━━ INTENT: SALUDO ━━━
Responde con energía y pregunta en qué le ayudas hoy.

═══════════════════════════════════════
MANEJO DE MONEDAS
═══════════════════════════════════════
- S/, soles, PEN → moneda: "PEN"
- $, dólares → moneda: "USD"
- Bs, bolivianos → moneda: "BOB"
- Sin moneda → asumir "PEN" y confirmarlo en respuesta

═══════════════════════════════════════
EJEMPLOS
═══════════════════════════════════════
"vendí 3 polos a 25 cada uno" → VENTA, producto: "polo", cantidad: 3, precio_unitario: 25.0, total: 75.0, moneda: "PEN"
"gasté 200 soles en pasajes a Bolivia" → GASTO, concepto: "pasajes a Bolivia", monto: 200.0, moneda: "PEN", categoria: "transporte"
"me quedan 15 jeans talla 32" → INVENTARIO, producto: "jean talla 32", cantidad: 15
"cuánto vendí hoy" → REPORTE, periodo: "hoy"
"""

ONBOARDING_PROMPTS = {
    1: """
Usuario NUEVO en Boti. PASO 1 del onboarding.
Responde con JSON donde "respuesta" sea bienvenida cálida que:
1. Lo salude y explique que Boti ayuda a controlar el negocio fácil por WhatsApp
2. En 2 líneas qué puede hacer (ventas, gastos, inventario)
3. Le pida el nombre de su negocio para empezar
intent: "ONBOARDING", datos: {"paso": 1}, requiere_confirmacion: false
""",
    2: """
PASO 2 del onboarding. Usuario ya dio el nombre del negocio.
Responde con JSON preguntando qué tipo de ropa vende (dama, caballero, niños, todo).
intent: "ONBOARDING", datos: {"paso": 2}, requiere_confirmacion: false
""",
    3: """
PASO 3 del onboarding. Usuario ya dijo qué tipo de ropa vende.
Responde con JSON preguntando a qué hora cierra su tienda (para enviar resumen diario).
intent: "ONBOARDING", datos: {"paso": 3}, requiere_confirmacion: false
""",
    4: """
PASO 4 del onboarding. Ya configuró todo.
Responde con JSON diciendo que está listo y mostrando cómo registrar la primera venta.
Ejemplo a darle: "Vendí 2 blusas a S/35 cada una"
intent: "ONBOARDING", datos: {"paso": 4}, requiere_confirmacion: false
""",
}


class GeminiService:
    def __init__(self):
        self.model = genai.GenerativeModel(
            model_name=settings.GEMINI_MODEL,
            generation_config=genai.GenerationConfig(
                temperature=0.3,
                top_p=0.8,
                max_output_tokens=512,
            ),
            system_instruction=SYSTEM_PROMPT,
        )
        logger.info(f"✅ GeminiService iniciado con modelo: {settings.GEMINI_MODEL}")

    def _parsear_respuesta(self, raw: str) -> dict:
        """Limpia y parsea la respuesta JSON de Gemini."""
        raw = raw.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        return json.loads(raw)

    async def procesar_mensaje(
        self,
        mensaje: str,
        historial: list[dict] = None,
        contexto_negocio: dict = None,
    ) -> dict:
        """
        Procesa un mensaje y retorna intent + datos + respuesta.
        
        Args:
            mensaje: Texto del usuario
            historial: [{"role": "user"|"model", "parts": [str]}]
            contexto_negocio: {"nombre": str, "tipo_ropa": str}
        """
        historial = historial or []
        contexto_negocio = contexto_negocio or {}

        mensaje_final = mensaje
        if contexto_negocio.get("nombre"):
            ctx = f"[Negocio: '{contexto_negocio['nombre']}', ropa: '{contexto_negocio.get('tipo_ropa', '')}'] "
            mensaje_final = ctx + mensaje

        try:
            chat = self.model.start_chat(history=historial)
            response = await chat.send_message_async(mensaje_final)
            return self._parsear_respuesta(response.text)

        except json.JSONDecodeError:
            logger.warning(f"[Gemini] JSON inválido en respuesta: {response.text[:200]}")
            return {
                "intent": "DESCONOCIDO",
                "datos": {},
                "respuesta": "No entendí bien. ¿Puedes decirme qué vendiste o en qué te ayudo? 😊",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }
        except Exception as e:
            logger.error(f"[Gemini] Error procesando mensaje: {e}")
            return {
                "intent": "ERROR",
                "datos": {},
                "respuesta": "Ups, tuve un problema técnico. Intenta de nuevo 🙏",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    async def procesar_onboarding(self, paso: int, mensaje_usuario: str = "") -> dict:
        """Genera la respuesta para cada paso del onboarding."""
        prompt = ONBOARDING_PROMPTS.get(paso, ONBOARDING_PROMPTS[1])
        if mensaje_usuario:
            prompt += f"\nEl usuario escribió: '{mensaje_usuario}'"

        try:
            response = await self.model.generate_content_async(prompt)
            return self._parsear_respuesta(response.text)
        except Exception as e:
            logger.error(f"[Gemini] Error en onboarding paso {paso}: {e}")
            return {
                "intent": "ONBOARDING",
                "datos": {"paso": paso},
                "respuesta": "¡Bienvenido a Boti! 👋 Para empezar, ¿cómo se llama tu negocio?",
                "requiere_confirmacion": False,
                "siguiente_paso": "esperar nombre del negocio",
            }

    async def generar_resumen_reporte(self, datos: dict) -> str:
        """Genera texto natural para un reporte de ventas/gastos."""
        prompt = f"""
Eres Boti. Genera un resumen de reporte para WhatsApp con estos datos:
{json.dumps(datos, ensure_ascii=False)}

Reglas:
- Máximo 5 líneas
- Emojis relevantes (💰📦📊)
- Mostrar ventas, gastos y ganancia neta
- Español peruano natural
- SIN asteriscos ni markdown (es WhatsApp)
- Terminar con frase motivadora corta

Solo responde el texto del mensaje, sin JSON.
"""
        try:
            response = await self.model.generate_content_async(prompt)
            return response.text.strip()
        except Exception:
            total_ventas = datos.get('total_ventas', 0)
            total_gastos = datos.get('total_gastos', 0)
            return (
                f"📊 Reporte de {datos.get('periodo', 'hoy')}:\n"
                f"💰 Ventas: S/{total_ventas:.2f}\n"
                f"📦 Gastos: S/{total_gastos:.2f}\n"
                f"✅ Ganancia: S/{total_ventas - total_gastos:.2f}"
            )


gemini_service = GeminiService()