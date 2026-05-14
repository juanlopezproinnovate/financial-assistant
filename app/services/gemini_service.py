"""
app/services/gemini_service.py
Motor NLP migrado a Groq — Llama 3.3 70b
Mismo nombre de archivo/clase para no cambiar imports en webhook.py

Cambios v2:
  - procesar_mensaje() ahora recibe y envía el historial real a Groq,
    dando al modelo memoria de corto plazo de los últimos 3 turnos.
  - Nuevo intent INCOMPLETO: el modelo lo usa cuando el mensaje es ambiguo
    y necesita pedir un dato adicional antes de registrar.

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

MEMORIA DE CONVERSACIÓN:
Recibirás el historial de los últimos turnos. Úsalo para entender mensajes cortos o ambiguos.
Ejemplos:
- Si antes dijiste "¿qué vendiste?" y ahora el usuario dice "un jean a 40", interpretar como VENTA.
- Si antes dijiste "¿cuántos?" y el usuario responde "3", usar ese contexto para completar datos.
- Si el usuario dice solo "1" tras una pregunta sobre cantidad, completar el registro con cantidad=1.

REGLA CRÍTICA DE FORMATO:
SIEMPRE responde SOLO con JSON válido. Sin texto antes ni después. Sin markdown.

Estructura OBLIGATORIA:
{
  "intent": "VENTA|GASTO|INVENTARIO|REPORTE|AYUDA|SALUDO|ELIMINAR_TRANSACCION|EDITAR_TRANSACCION|INCOMPLETO|DESCONOCIDO",
  "datos": {},
  "respuesta": "texto para WhatsApp",
  "requiere_confirmacion": false,
  "siguiente_paso": ""
}

INTENTS:

INCOMPLETO — el mensaje es ambiguo y falta información crítica para registrar algo.
Úsalo cuando el usuario dice algo como: "anota", "registra", "apunta algo", sin dar detalles.
NO lo uses si ya hay contexto en el historial que permita inferir los datos.
datos: {
  "contexto_parcial": str,    — lo que sí se entendió, ej: "quiere registrar algo"
  "campo_faltante": str       — qué necesitas: "tipo" | "monto" | "producto" | "cantidad"
}
respuesta: pregunta corta y directa para obtener el dato que falta.

ELIMINAR_TRANSACCION — frases: eliminar transaccion, borrar la ultima venta, me equivoque borra eso
datos: {}

EDITAR_TRANSACCION — frases: editar transaccion, cambiar monto de la venta, quiero modificar un gasto
datos: {}

VENTA — frases: vendí, vendiste, vendimos, salió una, me llevaron, acabo de vender
datos: {
  "producto": str,             — nombre específico tal como lo dijo el usuario. Ej: "polo azul talla M"
  "producto_descripcion": str, — detalles adicionales si los menciona (color, talla, modelo).
  "cantidad": int,
  "precio_unitario": float,
  "total": float,
  "moneda": "PEN|USD|BOB|CLP",
  "fecha": "YYYY-MM-DD",       — solo si el usuario especificó fecha
  "hora": "HH:MM:SS"           — solo si el usuario especificó hora
}
Si falta precio o cantidad, pídelos en "respuesta". No inventes valores.
IMPORTANTE: extrae el nombre del producto tan específico como el usuario lo diga.
"vendí un polo azul talla M" → producto: "polo azul talla M"
"vendí 3 polos" → producto: "polo" (sin más info disponible)
Si el historial ya tiene producto y monto, y el usuario responde solo la cantidad, completa el registro.

GASTO — frases: gasté, pagué, compré mercadería, flete, pasajes, alquiler
datos: {
  "concepto": str,
  "monto": float,
  "moneda": "PEN|USD|BOB|CLP",
  "categoria": "mercaderia|transporte|local|servicios|otros",
  "fecha": "YYYY-MM-DD",
  "hora": "HH:MM:SS"
}

FECHAS Y HORAS:
Si el usuario especifica fecha (ayer, hace 3 días, etc.), calcúlala basándote en la "Fecha de hoy" provista y devuelve "fecha".
Si el usuario especifica hora, devuelve "hora".
Si NO especifica fecha o hora, simplemente OMITE esos campos en el JSON.

INVENTARIO — frases: tengo, me quedan, llegaron, entró mercadería, agregué stock
datos: {
  "producto": str,
  "producto_descripcion": str,
  "cantidad": int,
  "tipo": "entrada|ajuste",
  "precio_costo": float,
  "precio_venta": float,
  "moneda": "PEN|USD|BOB|CLP"
}

REPORTE — frases: cómo voy, cuánto vendí, resumen, total, mis ventas
datos: {"periodo": "hoy|ayer|semana|mes"}

SALUDO — hola, buenas, buenos días, qué tal
Responde con energía y pregunta en qué ayudas.

MONEDAS:
S/, soles → "PEN"
$, dólares → "USD"
Bs, bolivianos → "BOB"
Pesos, CLP → "CLP"
Sin moneda → asumir "PEN"

EJEMPLOS:
"vendí 3 polos azul talla M a 25 soles" → VENTA, producto:"polo azul talla M", cantidad:3, precio_unitario:25, total:75, moneda:PEN
"vendí 3 polos a 25 soles" → VENTA, producto:"polo", cantidad:3, precio_unitario:25, total:75, moneda:PEN
"gasté 200 en pasajes a Bolivia" → GASTO, concepto:pasajes Bolivia, monto:200, moneda:PEN, categoria:transporte
"llegaron 50 blusas rojas talla S a S/15" → INVENTARIO, tipo:entrada, producto:"blusa roja talla S", cantidad:50, precio_costo:15, moneda:PEN
"tengo 20 pantalones" → INVENTARIO, tipo:ajuste, producto:"pantalón", cantidad:20
"cuánto vendí hoy" → REPORTE, periodo:hoy
"borra esa venta" → ELIMINAR_TRANSACCION
"quiero modificar el monto" → EDITAR_TRANSACCION
"anota" → INCOMPLETO, campo_faltante:"tipo"
"hola" → SALUDO

EJEMPLOS CON HISTORIAL (memoria de corto plazo):
Historial: user="anota", assistant="¿qué necesitas anotar, una venta o un gasto?"
Mensaje actual: "40 soles de un jean"
→ VENTA, producto:"jean", total:40, moneda:PEN — preguntar cantidad si no se especificó.

Historial: user="40 soles de un jean", assistant="Vendiste un jean a S/40. ¿Cuántos jeans vendiste?"
Mensaje actual: "1"
→ VENTA, producto:"jean", cantidad:1, precio_unitario:40, total:40, moneda:PEN — registro completo.
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
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group()
        return json.loads(raw)

    # ──────────────────────────────────────────────
    #  CORE: procesar mensaje principal
    # ──────────────────────────────────────────────

    async def procesar_mensaje(
        self,
        mensaje: str,
        historial: list[dict] = None,
        contexto_negocio: dict = None,
    ) -> dict:
        """
        Procesa el mensaje del comerciante usando el historial de corto plazo.

        historial: lista de dicts [{role: "user"|"assistant", content: str}, ...]
                   con los últimos N turnos. Se inserta entre el system prompt
                   y el mensaje actual para dar contexto al modelo.
        """
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

        # Construir el array de messages:
        # system → historial de turnos anteriores → mensaje actual
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(historial)
        messages.append({"role": "user", "content": mensaje_final})

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=messages,
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

    # ──────────────────────────────────────────────
    #  STOCK: resolver producto para descuento
    # ──────────────────────────────────────────────

    async def resolver_producto_venta(
        self,
        nombre_extraido: str,
        candidatos: list[dict],
    ) -> dict:
        """
        Dado el nombre que el NLP extrajo y una lista de candidatos
        de la BD (ya filtrados por pg_trgm), decide cuál es el match.

        candidatos: lista de dicts con {id, nombre, nombre_variantes, similitud}
        ordenados por similitud DESC.

        Retorna:
        {
          "match": "exacto" | "parcial" | "ninguno",
          "producto_id": uuid | None,
          "candidatos_ids": [uuid, ...],
        }
        """
        if not candidatos:
            return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

        if len(candidatos) == 1 and candidatos[0].get("similitud", 0) >= 0.6:
            return {
                "match": "exacto",
                "producto_id": candidatos[0]["id"],
                "candidatos_ids": [],
            }

        lista_txt = "\n".join(
            f'{i+1}. "{c["nombre"]}"' for i, c in enumerate(candidatos)
        )
        prompt = (
            f'El comerciante dijo que vendió: "{nombre_extraido}"\n\n'
            f"Estos son los productos registrados en su catálogo:\n{lista_txt}\n\n"
            f"¿Alguno de estos productos es claramente el mismo que mencionó el comerciante?\n"
            f"Responde SOLO con JSON:\n"
            f'{{"match": "exacto"|"parcial"|"ninguno", "indice": 1..N|null}}\n'
            f"- exacto: hay UNO que claramente es el mismo producto.\n"
            f"- parcial: hay varios que podrían serlo, no se puede decidir solo.\n"
            f"- ninguno: ninguno coincide con lo que dijo el comerciante.\n"
            f"indice: número del producto si match=exacto, null en cualquier otro caso."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador de productos. Responde SOLO con JSON válido.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            resultado = self._parsear(response.choices[0].message.content)
            match_tipo = resultado.get("match", "ninguno")
            indice = resultado.get("indice")

            if match_tipo == "exacto" and indice and 1 <= indice <= len(candidatos):
                return {
                    "match": "exacto",
                    "producto_id": candidatos[indice - 1]["id"],
                    "candidatos_ids": [],
                }
            elif match_tipo == "parcial":
                return {
                    "match": "parcial",
                    "producto_id": None,
                    "candidatos_ids": [c["id"] for c in candidatos],
                }
            else:
                return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

        except Exception as e:
            logger.error(f"[Groq] resolver_producto_venta error: {e}")
            if candidatos:
                return {
                    "match": "parcial",
                    "producto_id": None,
                    "candidatos_ids": [c["id"] for c in candidatos],
                }
            return {"match": "ninguno", "producto_id": None, "candidatos_ids": []}

    async def confirmar_producto_nuevo(
        self,
        nombre: str,
        precio: float | None,
        cantidad: int,
    ) -> str:
        """
        Genera el mensaje de confirmación para crear un producto nuevo
        que no estaba en el catálogo.
        """
        precio_txt = f" con precio S/{precio:.2f}" if precio else ""
        prompt = (
            f'Genera un mensaje corto de WhatsApp (máximo 2 líneas) para preguntarle '
            f'al comerciante si quiere agregar "{nombre}"{precio_txt} a su catálogo, '
            f"dado que acaba de vender {cantidad} unidad(es) y no estaba registrado. "
            f"Habla en español peruano cálido. Termina con (sí/no). Sin markdown."
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
            return (
                f'No tengo "{nombre}" en tu catálogo. '
                f"¿Lo agrego{precio_str}? (sí/no)"
            )

    # ──────────────────────────────────────────────
    #  ONBOARDING
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    #  REPORTES
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    #  EDICIÓN DE TRANSACCIONES
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    #  ONBOARDING: extractores de datos
    # ──────────────────────────────────────────────

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
                    {
                        "role": "system",
                        "content": (
                            "Eres un extractor de datos preciso. "
                            "Responde SOLO con el valor solicitado, sin texto adicional."
                        ),
                    },
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
            f"Responde solo el código, sin explicaciones."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador. Responde SOLO con: PEN, CLP o PEN,CLP",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=16,
            )
            resultado = response.choices[0].message.content.strip().upper().replace(" ", "")
            if resultado == "CLP,PEN":
                resultado = "PEN,CLP"
            if resultado in ("PEN", "CLP", "PEN,CLP"):
                logger.info(f"[Groq] extraer_monedas → '{resultado}'")
                return resultado
            raise ValueError(f"Formato inesperado: {resultado}")
        except Exception as e:
            logger.warning(f"[Groq] extraer_monedas fallback por error: {e}")
            msg_lower = mensaje.lower()
            tiene_pen   = any(w in msg_lower for w in ["sol", "soles", "pen", "peruano", "1", "uno", "primero"])
            tiene_clp   = any(w in msg_lower for w in ["peso", "pesos", "clp", "chileno", "2", "dos", "segundo"])
            tiene_ambas = any(w in msg_lower for w in ["ambas", "los dos", "todo", "3", "tres", "ambos"])
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
                    {
                        "role": "system",
                        "content": (
                            "Eres un asistente que responde SOLO con arrays JSON válidos. "
                            "Sin texto adicional, sin markdown."
                        ),
                    },
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
                logger.info(f"[Groq] generar_categorias tipo='{tipo_ropa}' → {categorias}")
                return {"categorias": categorias}
            return {"categorias": fallback}
        except json.JSONDecodeError as e:
            logger.error(f"[Groq] generar_categorias JSON inválido: {e}")
            return {"categorias": fallback}
        except Exception as e:
            logger.error(f"[Groq] generar_categorias error: {e}")
            return {"categorias": fallback}
    
    async def extraer_producto_inventario(self, mensaje: str) -> dict:
        """
        Dado un mensaje en lenguaje libre, extrae los datos de un producto
        para carga de inventario durante el onboarding.
        Retorna dict con claves: nombre, talla, precio, cantidad, etiqueta
        (los que no se mencionen llegan como None).
        """
        prompt = (
            f'Un comerciante de ropa en Tacna escribió esto para registrar un producto:\n'
            f'"{mensaje}"\n\n'
            f'Extrae los datos y responde SOLO con JSON válido, sin texto adicional:\n'
            f'{{\n'
            f'  "nombre": "nombre del producto capitalizado, ej: Polo Básico",\n'
            f'  "talla": "talla en mayúsculas, ej: M, XL, 28, TALLA ÚNICA",\n'
            f'  "precio": número decimal o null,\n'
            f'  "cantidad": número entero o null,\n'
            f'  "etiqueta": "etiqueta corta o null si no se menciona o dice no"\n'
            f'}}\n\n'
            f'Reglas:\n'
            f'- nombre: capitaliza cada palabra. Si no se menciona → null.\n'
            f'- talla: siempre en MAYÚSCULAS. Si no se menciona → null.\n'
            f'- precio: solo el número, sin símbolo de moneda. Si no se menciona → null.\n'
            f'- cantidad: solo el número entero. Si no se menciona → null.\n'
            f'- etiqueta: si el usuario escribe "no", "ninguna", "sin etiqueta" → null.\n'
            f'- Si el mensaje mezcla precio y cantidad, el precio suele ser mayor.\n'
            f'Ejemplos:\n'
            f'"polo azul M precio 35 stock 10" → {{"nombre":"Polo Azul","talla":"M","precio":35.0,"cantidad":10,"etiqueta":null}}\n'
            f'"blusa floral talla S a 49.90 tengo 5 etiqueta verano24" → {{"nombre":"Blusa Floral","talla":"S","precio":49.90,"cantidad":5,"etiqueta":"verano24"}}\n'
            f'"jean slim 28 25 soles 8 unidades" → {{"nombre":"Jean Slim","talla":"28","precio":25.0,"cantidad":8,"etiqueta":null}}'
        )
        fallback = {"nombre": None, "talla": None, "precio": None, "cantidad": None, "etiqueta": None}
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres un extractor de datos de productos de ropa. "
                            "Responde SOLO con JSON válido, sin texto adicional ni markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=128,
            )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            resultado = json.loads(raw)

            # Normalizar tipos por seguridad
            if resultado.get("precio") is not None:
                resultado["precio"] = float(resultado["precio"])
            if resultado.get("cantidad") is not None:
                resultado["cantidad"] = int(resultado["cantidad"])
            if isinstance(resultado.get("talla"), str):
                resultado["talla"] = resultado["talla"].upper()
            if isinstance(resultado.get("nombre"), str):
                resultado["nombre"] = resultado["nombre"].strip().title()
            if isinstance(resultado.get("etiqueta"), str):
                if self._es_omision_etiqueta(resultado["etiqueta"]):
                    resultado["etiqueta"] = None

            logger.info(f"[Groq] extraer_producto_inventario → {resultado}")
            return resultado

        except json.JSONDecodeError as e:
            logger.error(f"[Groq] extraer_producto_inventario JSON inválido: {e}")
            return fallback
        except Exception as e:
            logger.error(f"[Groq] extraer_producto_inventario error: {e}")
            return fallback

    def _es_omision_etiqueta(self, texto: str) -> bool:
        omisiones = {"no", "ninguno", "ninguna", "omitir", "saltar", "-", "n/a", "nada", "sin etiqueta"}
        return texto.strip().lower() in omisiones
    
    async def sugerir_categoria_producto(
        self,
        nombre_producto: str,
        categorias_negocio: list[str],
    ) -> dict:
        """
        Dado el nombre de un producto y las categorías existentes del negocio,
        intenta hacer match semántico y devuelve la categoría sugerida o None.
        
        Retorna:
        {
        "match": true | false,
        "categoria": "nombre de la categoría" | null
        }
        """
        if not categorias_negocio:
            return {"match": False, "categoria": None}

        lista = ", ".join(f'"{c}"' for c in categorias_negocio)
        prompt = (
            f'Producto: "{nombre_producto}"\n'
            f'Categorías disponibles: [{lista}]\n\n'
            f'¿A cuál de esas categorías pertenece este producto?\n'
            f'Responde SOLO con JSON válido:\n'
            f'{{"match": true|false, "categoria": "nombre exacto de la categoría o null"}}\n\n'
            f'Reglas:\n'
            f'- match true solo si hay una categoría claramente correcta.\n'
            f'- categoria debe ser el nombre EXACTO de una de la lista, o null si match=false.\n'
            f'- Ejemplos: "Polo Urbanno" → "Polos"; "Jean Slim" → "Jeans"; "Chompa lana" → "Chompas"\n'
            f'- Si no hay ninguna que aplique claramente → match: false, categoria: null'
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador de productos de ropa. Responde SOLO con JSON válido.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=64,
            )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            resultado = json.loads(raw)
            # Validar que categoria sea una de la lista
            if resultado.get("match") and resultado.get("categoria") not in categorias_negocio:
                resultado["match"] = False
                resultado["categoria"] = None
            logger.info(f"[Groq] sugerir_categoria '{nombre_producto}' → {resultado}")
            return resultado
        except Exception as e:
            logger.error(f"[Groq] sugerir_categoria error: {e}")
            return {"match": False, "categoria": None}


gemini_service = GeminiService()