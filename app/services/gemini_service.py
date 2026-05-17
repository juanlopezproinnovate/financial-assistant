"""
app/services/gemini_service.py
Motor NLP migrado a Groq — Llama 3.3 70b

Cambios v3:
  - VENTA, GASTO e INVENTARIO ahora soportan múltiples items (1 a 5 máximo)
  - El campo "items" reemplaza a "datos" para estos tres intents
  - El resto de intents (REPORTE, ELIMINAR, EDITAR, etc.) siguen usando "datos"
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

MEMORIA DE CONVERSACIÓN:
Recibirás el historial de los últimos turnos. Úsalo para entender mensajes cortos o ambiguos.

REGLA CRÍTICA DE FORMATO:
SIEMPRE responde SOLO con JSON válido. Sin texto antes ni después. Sin markdown.

Estructura OBLIGATORIA:
{
  "intent": "VENTA|GASTO|INVENTARIO|CATALOGO|REPORTE|AYUDA|SALUDO|ELIMINAR_TRANSACCION|EDITAR_TRANSACCION|INCOMPLETO|DESCONOCIDO",
  "items": [],
  "datos": {},
  "respuesta": "texto para WhatsApp",
  "requiere_confirmacion": false,
  "siguiente_paso": ""
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTENTS CON MÚLTIPLES ITEMS (1 a 5 máximo)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Para VENTA, GASTO e INVENTARIO SIEMPRE usa el campo "items" como array.
Aunque sea un solo producto, ponlo dentro del array.
El campo "datos" queda vacío {} para estos tres intents.

── VENTA ──────────────────────────────────
Frases: vendí, vendiste, vendimos, salió una, me llevaron, acabo de vender

"items": [
  {
    "producto": str,             — nombre específico tal como lo dijo el usuario
    "producto_descripcion": str, — detalles adicionales (color, talla, modelo)
    "cantidad": int,
    "precio_unitario": float,
    "total": float,
    "moneda": "PEN|USD|BOB|CLP",
    "fecha": "YYYY-MM-DD",       — solo si el usuario especificó fecha
    "hora": "HH:MM:SS"           — solo si el usuario especificó hora
  }
]

REGLAS DE PRECIO PARA VENTA:
- El precio mencionado SIN "cada uno" / "c/u" / "por unidad" es SIEMPRE el TOTAL.
- precio_unitario = total / cantidad
- Si dice "vendí 2 polos a 70 soles" → total:70, precio_unitario:35
- Si dice "vendí 2 polos a 35 cada uno" → precio_unitario:35, total:70
- Si el usuario NO menciona precio → omite precio_unitario y total (ponlos null, NO 0).
- NUNCA inventes ni asumas un precio si el usuario no lo mencionó.
- Si falta precio o cantidad de algún item → pídelos en "respuesta"

SEÑALES DE PRECIO UNITARIO (precio se multiplica por cantidad):
"cada uno", "c/u", "por unidad", "uno", "cada", "x unidad"

SEÑALES DE PRECIO TOTAL (precio se divide entre cantidad):
ninguna señal especial, precio mencionado solo → siempre es total

EJEMPLOS VENTA:
"vendí 2 polos a 70 soles"
→ items:[{producto:"polo", cantidad:2, precio_unitario:35, total:70, moneda:"PEN"}]

"vendí 2 polos a 35 cada uno"
→ items:[{producto:"polo", cantidad:2, precio_unitario:35, total:70, moneda:"PEN"}]

"vendí 1 jean slim a 120"
→ items:[{producto:"jean slim", cantidad:1, precio_unitario:120, total:120, moneda:"PEN"}]

"vendí 2 polos a 70 y 3 blusas a 90 soles"
→ items:[
    {producto:"polo", cantidad:2, precio_unitario:35, total:70, moneda:"PEN"},
    {producto:"blusa", cantidad:3, precio_unitario:30, total:90, moneda:"PEN"}
  ]

"vendí 2 polos a 35 cada uno, 1 blusa a 50 y 4 shorts a 25 c/u"
→ items:[
    {producto:"polo", cantidad:2, precio_unitario:35, total:70, moneda:"PEN"},
    {producto:"blusa", cantidad:1, precio_unitario:50, total:50, moneda:"PEN"},
    {producto:"short", cantidad:4, precio_unitario:25, total:100, moneda:"PEN"}
  ]

── GASTO ──────────────────────────────────
Frases: gasté, he gastado, gaste, pagué, he pagado, pague, 
        compré mercadería, flete, pasajes, alquiler, invertí, 
        salió, me salió, costó, me costó, desembolsé, egreso

"items": [
  {
    "concepto": str,
    "monto": float,
    "moneda": "PEN|USD|BOB|CLP",
    "categoria": "mercaderia|transporte|local|servicios|otros",
    "fecha": "YYYY-MM-DD",
    "hora": "HH:MM:SS"
  }
]

EJEMPLOS GASTO:
"gasté 200 en pasajes y 50 en almuerzo"
→ items:[
    {concepto:"pasajes", monto:200, moneda:"PEN", categoria:"transporte"},
    {concepto:"almuerzo", monto:50, moneda:"PEN", categoria:"otros"}
  ]

"pagué 1500 de alquiler y 300 de luz"
→ items:[
    {concepto:"alquiler", monto:1500, moneda:"PEN", categoria:"local"},
    {concepto:"luz", monto:300, moneda:"PEN", categoria:"servicios"}
  ]

── INVENTARIO ──────────────────────────────
Frases: tengo, me quedan, llegaron, entró mercadería, agregué stock

"items": [
  {
    "producto": str,
    "producto_descripcion": str,
    "cantidad": int,
    "tipo": "entrada|ajuste",
    "precio_costo": float,
    "precio_venta": float,
    "moneda": "PEN|USD|BOB|CLP"
  }
]

EJEMPLOS INVENTARIO:
"llegaron 50 blusas rojas talla S a S/15 y 30 jeans slim a S/45"
→ items:[
    {producto:"blusa roja talla S", cantidad:50, tipo:"entrada", precio_costo:15, moneda:"PEN"},
    {producto:"jean slim", cantidad:30, tipo:"entrada", precio_costo:45, moneda:"PEN"}
  ]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OTROS INTENTS (usan "datos", "items" queda [])
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CATALOGO — frases: qué productos tengo, cuáles son mis productos,
           muéstrame mi catálogo, cuántos productos tengo,
           lista de productos, mis productos, qué tengo registrado,
           que tengo en inventario, qué productos tengo en mi inventario,
           muéstrame mi inventario, qué hay en mi inventario,
           cuántos productos tengo en inventario, ver inventario,
           mi inventario, pásame mi catálogo, pasame mi catalogo,
           envíame mi inventario, enviame mi inventario,
           quiero ver mi inventario, mi catalogo

datos: {"filtro": str o null}  ← null si no filtra, "blusas" si dice "mis blusas"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLA DE DISTINCIÓN INVENTARIO vs CATALOGO:
- INVENTARIO → el usuario quiere AGREGAR o ACTUALIZAR stock
  Señales: "llegaron", "entró", "agregué", "añadir producto", 
           "registrar producto", "nuevo producto", "quiero agregar"
- CATALOGO → el usuario quiere VER o CONSULTAR lo que tiene
  Señales: "qué tengo", "muéstrame", "ver", "cuántos tengo",
           "listar", "mis productos", "inventario" cuando es consulta
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INCOMPLETO — mensaje ambiguo sin info crítica
datos: {
  "contexto_parcial": str,
  "campo_faltante": str   — "tipo"|"monto"|"producto"|"cantidad"
}

ELIMINAR_TRANSACCION — frases: eliminar, borrar la ultima venta, me equivoqué
datos: {}

EDITAR_TRANSACCION — frases: editar, cambiar monto, modificar un gasto
datos: {}

REPORTE — frases: cómo voy, cuánto vendí, resumen, total, mis ventas
datos: {"periodo": "hoy|ayer|semana|mes"}

SALUDO — hola, buenas, buenos días
Responde con energía y pregunta en qué ayudas.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLAS GENERALES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MONEDAS:
S/, soles → "PEN"
$, dólares → "USD"
Bs, bolivianos → "BOB"
Pesos, CLP → "CLP"
Sin moneda → asumir "PEN"

FECHAS Y HORAS:
Si el usuario especifica fecha (ayer, hace 3 días), calcúlala con la "Fecha de hoy" provista.
Si NO especifica → omite los campos fecha y hora del item.

LÍMITE DE ITEMS:
Máximo 5 items por mensaje. Si el usuario menciona más de 5, registra los primeros 5
y avísale en "respuesta" que el resto debe enviarlo en otro mensaje.

MEMORIA DE CONVERSACIÓN — EJEMPLOS:
Historial: user="anota", assistant="¿qué necesitas anotar, una venta o un gasto?"
Mensaje actual: "40 soles de un jean"
→ VENTA, items:[{producto:"jean", total:40, moneda:"PEN"}], pedir cantidad si falta.

Historial: user="40 soles de un jean", assistant="¿Cuántos jeans vendiste?"
Mensaje actual: "1"
→ VENTA, items:[{producto:"jean", cantidad:1, precio_unitario:40, total:40, moneda:"PEN"}]
"""

ONBOARDING_PROMPTS = {
    1: """Responde SOLO con JSON. Usuario nuevo en Boti, paso 1.
La "respuesta" debe ser bienvenida cálida que: salude, explique en 2 líneas qué hace Boti (ventas/gastos/inventario por WhatsApp), y pida el nombre del negocio.
{"intent":"ONBOARDING","datos":{},"items":[],"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    2: """Responde SOLO con JSON. Paso 2 del onboarding.
La "respuesta" pregunta qué tipo de ropa vende (dama, caballero, niños, todo).
{"intent":"ONBOARDING","datos":{},"items":[],"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    3: """Responde SOLO con JSON. Paso 3 del onboarding.
La "respuesta" pregunta a qué hora cierra la tienda para enviar resumen diario.
{"intent":"ONBOARDING","datos":{},"items":[],"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    4: """Responde SOLO con JSON. Paso 4 del onboarding, todo configurado.
La "respuesta" celebra que está listo y muestra ejemplo: "Vendí 2 blusas a S/35 cada una".
{"intent":"ONBOARDING","datos":{},"items":[],"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",
}


class GeminiService:

    def _parsear(self, raw: str) -> dict:
        raw = raw.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group()
        return json.loads(raw)

    # ──────────────────────────────────────────────────────
    #  CORE: procesar mensaje principal
    # ──────────────────────────────────────────────────────

    async def procesar_mensaje(
        self,
        mensaje: str,
        historial: list[dict] = None,
        contexto_negocio: dict = None,
    ) -> dict:
        contexto_negocio = contexto_negocio or {}
        historial = historial or []

        from zoneinfo import ZoneInfo
        zona_str = contexto_negocio.get("zona_horaria", "America/Lima")
        try:
            tz = ZoneInfo(zona_str)
        except Exception:
            tz = ZoneInfo("America/Lima")
        hoy_local = datetime.datetime.now(tz).date()

        mensaje_final = f"[Fecha de hoy: {hoy_local}] {mensaje}"
        if contexto_negocio.get("nombre"):
            mensaje_final = (
                f"[Negocio: {contexto_negocio['nombre']}, "
                f"ropa: {contexto_negocio.get('tipo_ropa', '')} | "
                f"Fecha de hoy: {hoy_local}] {mensaje}"
            )

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(historial)
        messages.append({"role": "user", "content": mensaje_final})

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=messages,
                temperature=0.2,
                max_tokens=1024,  # subimos tokens para soportar múltiples items
            )
            raw = response.choices[0].message.content
            resultado = self._parsear(raw)

            # Normalizar: asegurar que siempre existan "items" y "datos"
            if "items" not in resultado:
                resultado["items"] = []
            if "datos" not in resultado:
                resultado["datos"] = {}

            # Compatibilidad: si el LLM puso datos en "datos" en vez de "items"
            # para VENTA/GASTO/INVENTARIO, migrarlo automáticamente
            intent = resultado.get("intent", "")
            if intent in ("VENTA", "GASTO", "INVENTARIO"):
                if not resultado["items"] and resultado["datos"]:
                    resultado["items"] = [resultado["datos"]]
                    resultado["datos"] = {}

            return resultado

        except json.JSONDecodeError:
            logger.warning(f"[Groq] JSON inválido: {raw[:200]}")
            return {
                "intent": "DESCONOCIDO",
                "items": [],
                "datos": {},
                "respuesta": "No entendí bien. ¿Puedes decirme qué vendiste o en qué te ayudo? 😊",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }
        except Exception as e:
            logger.error(f"[Groq] Error procesando mensaje: {e}")
            return {
                "intent": "ERROR",
                "items": [],
                "datos": {},
                "respuesta": "Ups, tuve un problema técnico. Intenta de nuevo 🙏",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    # ──────────────────────────────────────────────────────
    #  STOCK: resolver producto para descuento
    # ──────────────────────────────────────────────────────

    async def resolver_producto_venta(
        self,
        nombre_extraido: str,
        candidatos: list[dict],
    ) -> dict:
        if not candidatos:
            return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

        if len(candidatos) == 1 and candidatos[0].get("similitud", 0) >= 0.6:
            return {
                "match": "exacto",
                "producto_id": candidatos[0]["id"],
                "candidatos_ids": [],
            }

        lista_txt = "\n".join(
            f'{i+1}. "{c["nombre"]}"' + (f' (Talla: {c["talla"]})' if c.get("talla") else "")
            for i, c in enumerate(candidatos)
        )
        prompt = (
            f'El comerciante dijo que vendió: "{nombre_extraido}"\n\n'
            f"Estos son los productos registrados en su catálogo:\n{lista_txt}\n\n"
            f"¿Alguno de estos productos es claramente el mismo que mencionó el comerciante?\n"
            f"Responde SOLO con JSON:\n"
            f'{{"match": "exacto"|"parcial"|"ninguno", "indice": 1..N|null}}\n'
            f"- exacto: hay UNO que claramente es el mismo producto.\n"
            f"- parcial: hay varios que podrían serlo.\n"
            f"- ninguno: ninguno coincide.\n"
            f"indice: número del producto si match=exacto, null en cualquier otro caso."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un clasificador de productos. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            resultado = self._parsear(response.choices[0].message.content)
            match_tipo = resultado.get("match", "ninguno")
            indice = resultado.get("indice")

            if match_tipo == "exacto" and indice and 1 <= indice <= len(candidatos):
                return {"match": "exacto", "producto_id": candidatos[indice - 1]["id"], "candidatos_ids": []}
            elif match_tipo == "parcial":
                return {"match": "parcial", "producto_id": None, "candidatos_ids": [c["id"] for c in candidatos]}
            else:
                return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

        except Exception as e:
            logger.error(f"[Groq] resolver_producto_venta error: {e}")
            if candidatos:
                return {"match": "parcial", "producto_id": None, "candidatos_ids": [c["id"] for c in candidatos]}
            return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

    async def extraer_datos_stock_inicial(self, mensaje: str) -> dict:
        prompt = (
            f"El usuario está registrando el stock inicial de un producto nuevo.\n"
            f'Mensaje del usuario: "{mensaje}"\n\n'
            f"Extrae la cantidad en stock, la talla (si la menciona) y el precio de compra (si lo menciona).\n"
            f'Responde SOLO con JSON válido:\n'
            f'{{"cantidad": int o null, "talla": str o null, "precio_costo": float o null}}'
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=64,
            )
            return self._parsear(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"[Groq] extraer_datos_stock_inicial error: {e}")
            return {"cantidad": None, "talla": None, "precio_costo": None}

    async def confirmar_producto_nuevo(self, nombre: str, precio: float | None, cantidad: int) -> str:
        precio_txt = f" con precio S/{precio:.2f}" if precio else ""
        prompt = (
            f'Genera un mensaje corto de WhatsApp (máximo 2 líneas) para preguntarle '
            f'al comerciante si quiere agregar "{nombre}"{precio_txt} a su catálogo, '
            f"dado que acaba de vender {cantidad} unidad(es) y no estaba registrado. "
            f"Habla en español peruano cálido. Termina con: ¿te interesa agregarlo (sí/no)? Sin markdown."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=80,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            precio_str = f" a S/{precio:.2f}" if precio else ""
            return f'No tengo "{nombre}" en tu catálogo. ¿Lo agrego{precio_str} (sí/no)?'

    # ──────────────────────────────────────────────────────
    #  ONBOARDING
    # ──────────────────────────────────────────────────────

    async def procesar_onboarding(self, paso: int, mensaje_usuario: str = "") -> dict:
        prompt = ONBOARDING_PROMPTS.get(paso, ONBOARDING_PROMPTS[1])
        if mensaje_usuario:
            prompt += f"\nEl usuario escribió: '{mensaje_usuario}'"

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres Boti, asistente de negocios por WhatsApp para "
                            "comerciantes de ropa en Tacna, Perú. "
                            "Habla en español peruano cálido y natural."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=256,
            )
            raw = response.choices[0].message.content
            return self._parsear(raw)

        except Exception as e:
            logger.error(f"[Groq] Error onboarding paso {paso}: {e}")
            fallbacks = {
                1: "¡Hola! Soy Boti 👋 Te ayudo a controlar ventas y gastos por WhatsApp. ¿Cómo se llama tu negocio?",
                2: "¿Qué tipo de ropa vendes principalmente? (dama, caballero, niños, todo)",
                3: "¿A qué hora cierras tu tienda? Te mando el resumen del día a esa hora 📊",
                4: "¡Todo listo! 🎉 Prueba escribiendo: 'Vendí 2 polos a S/25 cada uno'",
            }
            return {
                "intent": "ONBOARDING",
                "items": [],
                "datos": {"paso": paso},
                "respuesta": fallbacks.get(paso, "¡Hola! ¿Cómo se llama tu negocio?"),
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    # ──────────────────────────────────────────────────────
    #  REPORTES
    # ──────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────
    #  EDICIÓN DE TRANSACCIONES
    # ──────────────────────────────────────────────────────

    async def interpretar_edicion(self, transaccion: dict, mensaje: str) -> dict:
        prompt = f"""Eres Boti. Un usuario quiere editar una transacción existente.
Transacción actual:
{json.dumps(transaccion, ensure_ascii=False)}

El usuario ha dicho: "{mensaje}"

Devuelve SOLO un JSON con los campos que deben actualizarse.
Posibles campos: "descripcion", "monto", "moneda", "tipo".
Si no entiendes qué cambiar, devuelve {{}}.
No incluyas texto adicional ni markdown."""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=150,
            )
            return self._parsear(response.choices[0].message.content.strip())
        except Exception as e:
            logger.error(f"[Groq] interpretar_edicion error: {e}")
            return {}

    # ──────────────────────────────────────────────────────
    #  ONBOARDING: extractores de datos
    # ──────────────────────────────────────────────────────

    async def extraer_dato(self, campo: str, mensaje: str) -> str:
        prompt = (
            f'El usuario escribió: "{mensaje}"\n\n'
            f"Extrae únicamente el valor correspondiente al campo: {campo}.\n"
            f"Responde SOLO con el valor extraído, sin explicaciones ni puntuación extra.\n"
            f"Si no puedes identificarlo con claridad, devuelve el texto tal como está, limpio."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un extractor de datos preciso. Responde SOLO con el valor solicitado."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            resultado = response.choices[0].message.content.strip().strip('"').strip("'")
            logger.info(f"[Groq] extraer_dato campo='{campo}' → '{resultado}'")
            return resultado if resultado else mensaje.strip()
        except Exception as e:
            logger.error(f"[Groq] extraer_dato error: {e}")
            return mensaje.strip()

    async def extraer_monedas(self, mensaje: str) -> str:
        prompt = (
            f'El usuario respondió sobre qué monedas acepta en su negocio: "{mensaje}"\n\n'
            f"Clasifica su respuesta y responde ÚNICAMENTE con uno de estos códigos:\n"
            f"PEN      → solo acepta soles peruanos\n"
            f"CLP      → solo acepta pesos chilenos\n"
            f"PEN,CLP  → acepta ambas monedas\n\n"
            f"Ejemplos:\n"
            f"'1' → PEN\n"
            f"'solo soles' → PEN\n"
            f"'soles noma' → PEN\n"
            f"'soles nomás' → PEN\n"
            f"'No, solo soles' → PEN\n"
            f"'2' → CLP\n"
            f"'solo pesos' → CLP\n"
            f"'3' → PEN,CLP\n"
            f"'ambas' → PEN,CLP\n"
            f"'los dos' → PEN,CLP\n\n"
            f"Responde solo el código, sin explicaciones."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un clasificador. Responde SOLO con: PEN, CLP o PEN,CLP"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=16,
            )
            resultado = response.choices[0].message.content.strip().upper().replace(" ", "")
            if resultado == "CLP,PEN":
                resultado = "PEN,CLP"
            if resultado in ("PEN", "CLP", "PEN,CLP"):
                return resultado
            raise ValueError(f"Formato inesperado: {resultado}")
        except Exception as e:
            logger.warning(f"[Groq] extraer_monedas fallback: {e}")
            msg_lower = mensaje.lower()
            tiene_pen   = any(w in msg_lower for w in ["sol", "soles", "pen", "peruano", "1", "uno", "primero"])
            tiene_clp   = any(w in msg_lower for w in ["peso", "pesos", "clp", "chileno", "2", "dos", "segundo"])
            tiene_ambas = any(w in msg_lower for w in ["ambas", "los dos", "todo", "3", "tres", "ambos"])
            tiene_negacion = any(w in msg_lower for w in ["no,", "solo soles", "noma", "nomás", "nomas"])

            if tiene_negacion and tiene_pen:
                return "PEN"
            if tiene_ambas or (tiene_pen and tiene_clp):
                return "PEN,CLP"
            if tiene_clp:
                return "CLP"
            return "PEN"

    async def generar_categorias_por_tipo_ropa(self, tipo_ropa: str) -> dict:
        prompt = (
            f'Un comerciante en Tacna, Perú vende: "{tipo_ropa}".\n\n'
            f"Genera exactamente 5 categorías de inventario para ese tipo de negocio.\n"
            f"Reglas:\n"
            f"- Nombres cortos (1-3 palabras), en español, con mayúscula inicial.\n"
            f"- Responde ÚNICAMENTE con un array JSON válido.\n"
            f"- Sin texto antes ni después, sin bloques de código markdown.\n\n"
            f'Formato exacto: ["Cat1", "Cat2", "Cat3", "Cat4", "Cat5"]'
        )
        fallback = ["Polos", "Pantalones", "Vestidos", "Accesorios", "Otros"]
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un asistente que responde SOLO con arrays JSON válidos."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=128,
            )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            categorias = json.loads(raw)
            if isinstance(categorias, list) and len(categorias) >= 3:
                categorias = [str(c).strip().title() for c in categorias[:5]]
                return {"categorias": categorias}
            return {"categorias": fallback}
        except Exception as e:
            logger.error(f"[Groq] generar_categorias error: {e}")
            return {"categorias": fallback}

    async def extraer_producto_inventario(self, mensaje: str, producto_actual: dict = None) -> dict:
        contexto_txt = ""
        if producto_actual:
            para_mostrar = {
                "nombre": producto_actual.get("nombre"),
                "talla": producto_actual.get("talla"),
                "precio_venta": producto_actual.get("precio_venta"),
                "cantidad": producto_actual.get("cantidad"),
                "precio_compra": producto_actual.get("precio_compra"),
            }
            contexto_txt = (
                f"El usuario está completando paso a paso los datos de un producto.\n"
                f"Actualmente tenemos:\n{json.dumps(para_mostrar, indent=2)}\n"
                f"Los campos en null son los que faltan.\n\n"
            )

        prompt = (
            f'{contexto_txt}'
            f'Un comerciante de ropa en Tacna escribió esto para registrar un producto:\n'
            f'"{mensaje}"\n\n'
            f'Extrae los datos y responde SOLO con JSON válido:\n'
            f'{{"nombre": str o null, "talla": str o null, "precio_venta": float o null, "cantidad": int o null, "precio_compra": float o null}}\n\n'
            f'REGLAS DE EXTRACCIÓN:\n'
            f'- "precio_venta" → precio al que se VENDE al cliente.\n'
            f'  Señales: "precio", "cuesta", "vale", "lo vendo a", "precio de venta", "vendo a", "sale a"\n'
            f'- "precio_compra" → precio al que se COMPRÓ la mercadería.\n'
            f'  Señales: "me costó", "costo", "compré a", "precio de compra", "lo compré a", "me salió"\n'
            f'- Si el mensaje menciona UN SOLO precio sin especificar tipo → siempre asignarlo a "precio_venta"\n'
            f'- Si ya se sabe el precio_venta por contexto y aparece otro precio → asignarlo a "precio_compra"\n'
            f'- "cantidad" → stock disponible.\n'
            f'  Señales: "tengo", "stock", "unidades", "en stock tengo", "me quedan", "hay"\n'
            f'EJEMPLOS:\n'
            f'- "jean básico XL que cuesta 23 soles" → precio_venta=23, precio_compra=null\n'
            f'- "polo talla M, precio 35, me costó 15" → precio_venta=35, precio_compra=15\n'
            f'- "blusa S, vale 50, lo compré a 20, tengo 10" → precio_venta=50, precio_compra=20, cantidad=10\n'
            f'- "short talla M, precio 55 soles, stock 29" → precio_venta=55, precio_compra=null, cantidad=29\n'
        )
        fallback = {"nombre": None, "talla": None, "precio_venta": None, "cantidad": None, "precio_compra": None}
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un extractor de datos de productos de ropa. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=128,
            )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            resultado = json.loads(raw)

            if resultado.get("precio_venta") is not None:
                try: resultado["precio_venta"] = float(resultado["precio_venta"])
                except: resultado["precio_venta"] = None
            if resultado.get("precio_compra") is not None:
                try: resultado["precio_compra"] = float(resultado["precio_compra"])
                except: resultado["precio_compra"] = None
            if resultado.get("cantidad") is not None:
                try: resultado["cantidad"] = int(resultado["cantidad"])
                except: resultado["cantidad"] = None
            if isinstance(resultado.get("talla"), str):
                resultado["talla"] = resultado["talla"].upper()
            if isinstance(resultado.get("nombre"), str):
                resultado["nombre"] = resultado["nombre"].strip().title()

            return resultado
        except Exception as e:
            logger.error(f"[Groq] extraer_producto_inventario error: {e}")
            return fallback

    def _es_omision_etiqueta(self, texto: str) -> bool:
        omisiones = {"no", "ninguno", "ninguna", "omitir", "saltar", "-", "n/a", "nada", "sin etiqueta"}
        return texto.strip().lower() in omisiones

    async def sugerir_categoria_producto(self, nombre_producto: str, categorias_negocio: list[str]) -> dict:
        if not categorias_negocio:
            return {"match": False, "categoria": None}

        lista = ", ".join(f'"{c}"' for c in categorias_negocio)
        prompt = (
            f'Producto: "{nombre_producto}"\n'
            f'Categorías disponibles: [{lista}]\n\n'
            f'¿A cuál pertenece este producto?\n'
            f'Responde SOLO con JSON: {{"match": true|false, "categoria": "nombre exacto o null"}}'
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un clasificador de productos. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            raw = re.sub(r"```json\s*|\s*```", "", response.choices[0].message.content.strip()).strip()
            resultado = json.loads(raw)
            if resultado.get("match") and resultado.get("categoria") not in categorias_negocio:
                resultado["match"] = False
                resultado["categoria"] = None
            return resultado
        except Exception as e:
            logger.error(f"[Groq] sugerir_categoria error: {e}")
            return {"match": False, "categoria": None}

    async def interpretar_accion_categoria(self, mensaje: str, categorias_actuales: list[str]) -> dict:
        lista_txt = "\n".join(f"{i+1}. {c}" for i, c in enumerate(categorias_actuales))
        prompt = f"""Estás ayudando a un comerciante a definir las CATEGORÍAS de su tienda de ropa.

Categorías actuales:
{lista_txt}

El comerciante respondió: "{mensaje}"

Clasifica su intención:
CONFIRMAR → acepta la lista. Señales: "listo", "ok", "bien", "sí", "dale", "perfecto", "ya".
AGREGAR   → quiere añadir una categoría genérica. valor: nombre de la categoría.
QUITAR    → quiere eliminar una. valor: número o nombre exacto.
PRODUCTO  → describe un producto específico con talla/color/modelo (no una categoría).
DESCONOCIDO → no se puede determinar.

Responde ÚNICAMENTE con JSON:
{{"accion": "CONFIRMAR|AGREGAR|QUITAR|PRODUCTO|DESCONOCIDO", "valor": null, "confianza": "alta|baja"}}"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un clasificador de intenciones. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=80,
            )
            resultado = self._parsear(response.choices[0].message.content.strip())
            accion = resultado.get("accion", "DESCONOCIDO").upper()
            valor  = resultado.get("valor")
            if accion == "QUITAR" and valor is not None:
                try: valor = int(str(valor).strip())
                except (ValueError, TypeError): pass
            return {"accion": accion, "valor": valor, "confianza": resultado.get("confianza", "baja")}
        except Exception as e:
            logger.error(f"[Groq] interpretar_accion_categoria error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["listo", "ok", "bien", "perfecto", "dale", "sí", "si", "ya"]):
                return {"accion": "CONFIRMAR", "valor": None, "confianza": "baja"}
            if any(w in msg for w in ["agregar", "añadir"]):
                partes = msg.split(maxsplit=1)
                return {"accion": "AGREGAR", "valor": partes[1].strip().title() if len(partes) > 1 else None, "confianza": "baja"}
            if any(w in msg for w in ["quitar", "eliminar", "borrar"]):
                nums = re.findall(r"\d+", msg)
                return {"accion": "QUITAR", "valor": int(nums[0]) if nums else None, "confianza": "baja"}
            return {"accion": "DESCONOCIDO", "valor": None, "confianza": "baja"}

    async def interpretar_accion_inventario(self, mensaje: str, producto_actual: dict) -> dict:
        producto_txt = (
            f"Nombre: {producto_actual.get('nombre', '?')}\n"
            f"Talla: {producto_actual.get('talla', '(no especificada)')}\n"
            f"Stock: {producto_actual.get('cantidad', '(no especificado)')}\n"
            f"Precio de Venta: {producto_actual.get('precio_venta', '?')}\n"
            f"Precio de Compra: {producto_actual.get('precio_compra', '(no especificado)')}\n"
            f"Categoría: {producto_actual.get('categoria', '(no especificada)')}"
        )
        prompt = f"""El bot confirmó este producto:
{producto_txt}

El usuario responde: "{mensaje}"

Clasifica:
TERMINAR     → guardar y terminar. Señales: "listo", "sí", "queda", "terminar", "ya".
AGREGAR_OTRO → guardar y agregar otro. Señales: "otro", "más", "agregar otro".
EDITAR       → cambiar algún campo. Extrae los nuevos valores.

Responde SOLO con JSON:
{{"accion": "TERMINAR|AGREGAR_OTRO|EDITAR|DESCONOCIDO", "cambios": {{"nombre": null, "talla": null, "cantidad": null, "precio_venta": null, "precio_compra": null, "categoria": null}}}}"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un extractor de intenciones. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=150,
            )
            raw = re.sub(r"```json\s*|\s*```", "", response.choices[0].message.content.strip()).strip()
            resultado = json.loads(raw)
            accion  = resultado.get("accion", "DESCONOCIDO").upper()
            cambios = resultado.get("cambios", {}) or {}

            if cambios.get("cantidad") is not None:
                try: cambios["cantidad"] = int(cambios["cantidad"])
                except: cambios["cantidad"] = None
            if cambios.get("precio_venta") is not None:
                try: cambios["precio_venta"] = float(cambios["precio_venta"])
                except: cambios["precio_venta"] = None
            if cambios.get("precio_compra") is not None:
                try: cambios["precio_compra"] = float(cambios["precio_compra"])
                except: cambios["precio_compra"] = None
            if isinstance(cambios.get("talla"), str):
                cambios["talla"] = cambios["talla"].strip().upper()
            if isinstance(cambios.get("nombre"), str):
                cambios["nombre"] = cambios["nombre"].strip().title()
            if isinstance(cambios.get("categoria"), str):
                cambios["categoria"] = cambios["categoria"].strip().title()

            return {"accion": accion, "cambios": cambios}
        except Exception as e:
            logger.error(f"[Groq] interpretar_accion_inventario error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["queda", "listo", "bien", "ok", "ya", "terminar", "sí", "si"]): return {"accion": "TERMINAR", "cambios": {}}
            if any(w in msg for w in ["otro", "mas", "más", "agrega"]): return {"accion": "AGREGAR_OTRO", "cambios": {}}
            if any(w in msg for w in ["mal", "error", "no", "corregir", "cambiar", "edita"]): return {"accion": "EDITAR", "cambios": {}}
            return {"accion": "DESCONOCIDO", "cambios": {}}

    async def extraer_nombre_y_talla(self, texto_producto: str) -> dict:
        prompt = (
            f'El comerciante mencionó este producto: "{texto_producto}"\n\n'
            f'Extrae el nombre SIN la talla, y la talla por separado.\n'
            f'Responde SOLO con JSON: {{"nombre": "nombre capitalizado sin talla", "talla": "talla en MAYÚSCULAS o null"}}'
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un extractor de datos de productos. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            raw = re.sub(r"```json\s*|\s*```", "", response.choices[0].message.content.strip()).strip()
            resultado = json.loads(raw)
            if isinstance(resultado.get("nombre"), str):
                resultado["nombre"] = resultado["nombre"].strip().title()
            if isinstance(resultado.get("talla"), str):
                resultado["talla"] = resultado["talla"].strip().upper()
            return resultado
        except Exception as e:
            logger.error(f"[Groq] extraer_nombre_y_talla error: {e}")
            return {"nombre": texto_producto.strip().title(), "talla": None}

    async def interpretar_decision_producto_nuevo(self, mensaje: str) -> dict:
        prompt = f"""El bot preguntó si quiere agregar un producto nuevo al catálogo.
El comerciante respondió: "{mensaje}"

Clasifica:
AGREGAR   → quiere agregar. Señales: "agregar", "sí", "si", "1", "dale", "ok".
CONTINUAR → seguir sin stock. Señales: "no", "seguir", "2", "omitir".
CANCELAR  → cancelar la venta. Señales: "cancelar", "cancela".
DESCONOCIDO → no se puede determinar.

Responde ÚNICAMENTE con JSON: {{"accion": "AGREGAR|CONTINUAR|CANCELAR|DESCONOCIDO"}}"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un clasificador de intenciones. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=32,
            )
            resultado = self._parsear(response.choices[0].message.content.strip())
            return {"accion": resultado.get("accion", "DESCONOCIDO").upper()}
        except Exception as e:
            logger.error(f"[Groq] interpretar_decision_producto_nuevo error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["agregar", "sí", "si", "1", "dale", "ok"]): return {"accion": "AGREGAR"}
            if any(w in msg for w in ["no", "seguir", "2", "omitir"]): return {"accion": "CONTINUAR"}
            if any(w in msg for w in ["cancelar", "cancela"]): return {"accion": "CANCELAR"}
            return {"accion": "DESCONOCIDO"}

    async def interpretar_formulario_producto_venta(self, mensaje: str, formulario_actual: dict) -> dict:
        formulario_txt = (
            f"Nombre: {formulario_actual.get('nombre', '?')}\n"
            f"Talla: {formulario_actual.get('talla', '(no especificada)')}\n"
            f"Stock: {formulario_actual.get('stock', '(no especificado)')}\n"
            f"Precio de Venta: {formulario_actual.get('precio_venta', '?')}\n"
            f"Precio de Compra: {formulario_actual.get('precio_compra', '(no especificado)')}"
        )
        prompt = f"""El bot mostró este formulario:
{formulario_txt}

El comerciante respondió: "{mensaje}"

GUARDAR  → acepta. Señales: "guardar", "listo", "sí", "ok", "dale", "confirmar".
CANCELAR → cancela.
EDITAR   → quiere cambiar algo. Extrae los nuevos valores.

Responde SOLO con JSON:
{{"accion": "GUARDAR|CANCELAR|EDITAR", "cambios": {{"nombre": null, "talla": null, "stock": null, "precio_venta": null, "precio_compra": null}}}}"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {"role": "system", "content": "Eres un extractor de intenciones. Responde SOLO con JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=128,
            )
            raw = re.sub(r"```json\s*|\s*```", "", response.choices[0].message.content.strip()).strip()
            resultado = json.loads(raw)
            accion  = resultado.get("accion", "DESCONOCIDO").upper()
            cambios = resultado.get("cambios", {}) or {}

            if cambios.get("stock") is not None:
                try: cambios["stock"] = int(cambios["stock"])
                except: cambios["stock"] = None
            if cambios.get("precio_venta") is not None:
                try: cambios["precio_venta"] = float(cambios["precio_venta"])
                except: cambios["precio_venta"] = None
            if cambios.get("precio_compra") is not None:
                try: cambios["precio_compra"] = float(cambios["precio_compra"])
                except: cambios["precio_compra"] = None
            if isinstance(cambios.get("talla"), str):
                cambios["talla"] = cambios["talla"].strip().upper()
            if isinstance(cambios.get("nombre"), str):
                cambios["nombre"] = cambios["nombre"].strip().title()

            return {"accion": accion, "cambios": cambios}
        except Exception as e:
            logger.error(f"[Groq] interpretar_formulario_producto_venta error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["guardar", "listo", "ok", "sí", "si", "dale", "bien"]): return {"accion": "GUARDAR", "cambios": {}}
            if any(w in msg for w in ["cancelar", "cancela"]): return {"accion": "CANCELAR", "cambios": {}}
            return {"accion": "EDITAR", "cambios": {}}


gemini_service = GeminiService()