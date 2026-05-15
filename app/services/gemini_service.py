"""
app/services/gemini_service.py
Motor NLP migrado a Groq â€” Llama 3.3 70b
Mismo nombre de archivo/clase para no cambiar imports en webhook.py

Cambios v2:
  - procesar_mensaje() ahora recibe y envÃ­a el historial real a Groq,
    dando al modelo memoria de corto plazo de los Ãºltimos 3 turnos.
  - Nuevo intent INCOMPLETO: el modelo lo usa cuando el mensaje es ambiguo
    y necesita pedir un dato adicional antes de registrar.

LÃ­mites Groq free: 14,400 RPD / 30 RPM
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

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  SYSTEM PROMPT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SYSTEM_PROMPT = """
Eres Quri, el asistente de negocios por WhatsApp para comerciantes de ropa de Tacna, PerÃº.
Tu misiÃ³n es ayudar a registrar ventas, gastos e inventario usando lenguaje natural.

PERSONALIDAD:
- Habla en espaÃ±ol peruano natural, cÃ¡lido y directo.
- Usa emojis con moderaciÃ³n (1-2 por mensaje).
- SÃ© conciso: mÃ¡ximo 3-4 lÃ­neas. Los comerciantes estÃ¡n ocupados.
- TutÃ©alo siempre.
- Si hay errores ortogrÃ¡ficos o spanglish, entiÃ©ndelo igual.

CONTEXTO:
Tacna es zona de comercio transfronterizo. Comerciantes compran en Bolivia, Chile y Puno.
Manejan ropa de dama, caballero, niÃ±os, zapatos, accesorios.
Monedas: Soles (S/), dÃ³lares ($), bolivianos (Bs).

MEMORIA DE CONVERSACIÃ“N:
RecibirÃ¡s el historial de los Ãºltimos turnos. Ãšsalo para entender mensajes cortos o ambiguos.
Ejemplos:
- Si antes dijiste "Â¿quÃ© vendiste?" y ahora el usuario dice "un jean a 40", interpretar como VENTA.
- Si antes dijiste "Â¿cuÃ¡ntos?" y el usuario responde "3", usar ese contexto para completar datos.
- Si el usuario dice solo "1" tras una pregunta sobre cantidad, completar el registro con cantidad=1.

REGLA CRÃTICA DE FORMATO:
SIEMPRE responde SOLO con JSON vÃ¡lido. Sin texto antes ni despuÃ©s. Sin markdown.

Estructura OBLIGATORIA:
{
  "intent": "VENTA|GASTO|INVENTARIO|REPORTE|AYUDA|SALUDO|ELIMINAR_TRANSACCION|EDITAR_TRANSACCION|INCOMPLETO|DESCONOCIDO",
  "datos": {},
  "respuesta": "texto para WhatsApp",
  "requiere_confirmacion": false,
  "siguiente_paso": ""
}

INTENTS:

INCOMPLETO â€” el mensaje es ambiguo y falta informaciÃ³n crÃ­tica para registrar algo.
Ãšsalo cuando el usuario dice algo como: "anota", "registra", "apunta algo", sin dar detalles.
NO lo uses si ya hay contexto en el historial que permita inferir los datos.
datos: {
  "contexto_parcial": str,    â€” lo que sÃ­ se entendiÃ³, ej: "quiere registrar algo"
  "campo_faltante": str       â€” quÃ© necesitas: "tipo" | "monto" | "producto" | "cantidad"
}
respuesta: pregunta corta y directa para obtener el dato que falta.

ELIMINAR_TRANSACCION â€” frases: eliminar transaccion, borrar la ultima venta, me equivoque borra eso
datos: {}

EDITAR_TRANSACCION â€” frases: editar transaccion, cambiar monto de la venta, quiero modificar un gasto
datos: {}

VENTA â€” frases: vendÃ­, vendiste, vendimos, saliÃ³ una, me llevaron, acabo de vender
datos: {
  "producto": str,             â€” nombre especÃ­fico tal como lo dijo el usuario. Ej: "polo azul talla M"
  "producto_descripcion": str, â€” detalles adicionales si los menciona (color, talla, modelo).
  "cantidad": int,
  "precio_unitario": float,
  "total": float,
  "moneda": "PEN|USD|BOB|CLP",
  "fecha": "YYYY-MM-DD",       â€” solo si el usuario especificÃ³ fecha
  "hora": "HH:MM:SS"           â€” solo si el usuario especificÃ³ hora
}
Si falta precio o cantidad, pÃ­delos en "respuesta". No inventes valores.
IMPORTANTE SOBRE PRECIOS:
Si el usuario dice "vendÃ­ 2 polos a 70 soles" y NO especifica "cada uno" o "c/u", ASUME que 70 es el TOTAL. Entonces: total:70, precio_unitario:35.
Si dice "vendÃ­ 2 polos a 35 cada uno", entonces: precio_unitario:35, total:70.

IMPORTANTE: extrae el nombre del producto tan especÃ­fico como el usuario lo diga.
"vendÃ­ un polo azul talla M" â†’ producto: "polo azul talla M"
"vendÃ­ 3 polos" â†’ producto: "polo" (sin mÃ¡s info disponible)
Si el historial ya tiene producto y monto, y el usuario responde solo la cantidad, completa el registro.

GASTO â€” frases: gastÃ©, paguÃ©, comprÃ© mercaderÃ­a, flete, pasajes, alquiler
datos: {
  "concepto": str,
  "monto": float,
  "moneda": "PEN|USD|BOB|CLP",
  "categoria": "mercaderia|transporte|local|servicios|otros",
  "fecha": "YYYY-MM-DD",
  "hora": "HH:MM:SS"
}

FECHAS Y HORAS:
Si el usuario especifica fecha (ayer, hace 3 dÃ­as, etc.), calcÃºlala basÃ¡ndote en la "Fecha de hoy" provista y devuelve "fecha".
Si el usuario especifica hora, devuelve "hora".
Si NO especifica fecha o hora, simplemente OMITE esos campos en el JSON.

INVENTARIO â€” frases: tengo, me quedan, llegaron, entrÃ³ mercaderÃ­a, agreguÃ© stock
datos: {
  "producto": str,
  "producto_descripcion": str,
  "cantidad": int,
  "tipo": "entrada|ajuste",
  "precio_costo": float,
  "precio_venta": float,
  "moneda": "PEN|USD|BOB|CLP"
}

REPORTE â€” frases: cÃ³mo voy, cuÃ¡nto vendÃ­, resumen, total, mis ventas
datos: {"periodo": "hoy|ayer|semana|mes"}

SALUDO â€” hola, buenas, buenos dÃ­as, quÃ© tal
Responde con energÃ­a y pregunta en quÃ© ayudas.

MONEDAS:
S/, soles â†’ "PEN"
$, dÃ³lares â†’ "USD"
Bs, bolivianos â†’ "BOB"
Pesos, CLP â†’ "CLP"
Sin moneda â†’ asumir "PEN"

EJEMPLOS:
"vendÃ­ 2 polos a 70 soles" â†’ VENTA, producto:"polo", cantidad:2, precio_unitario:35, total:70, moneda:PEN
"vendÃ­ 3 polos a 25 soles cada uno" â†’ VENTA, producto:"polo", cantidad:3, precio_unitario:25, total:75, moneda:PEN
"gastÃ© 200 en pasajes a Bolivia" â†’ GASTO, concepto:pasajes Bolivia, monto:200, moneda:PEN, categoria:transporte
"llegaron 50 blusas rojas talla S a S/15" â†’ INVENTARIO, tipo:entrada, producto:"blusa roja talla S", cantidad:50, precio_costo:15, moneda:PEN
"tengo 20 pantalones" â†’ INVENTARIO, tipo:ajuste, producto:"pantalÃ³n", cantidad:20
"cuÃ¡nto vendÃ­ hoy" â†’ REPORTE, periodo:hoy
"borra esa venta" â†’ ELIMINAR_TRANSACCION
"quiero modificar el monto" â†’ EDITAR_TRANSACCION
"anota" â†’ INCOMPLETO, campo_faltante:"tipo"
"hola" â†’ SALUDO

EJEMPLOS CON HISTORIAL (memoria de corto plazo):
Historial: user="anota", assistant="Â¿quÃ© necesitas anotar, una venta o un gasto?"
Mensaje actual: "40 soles de un jean"
â†’ VENTA, producto:"jean", total:40, moneda:PEN â€” preguntar cantidad si no se especificÃ³.

Historial: user="40 soles de un jean", assistant="Vendiste un jean a S/40. Â¿CuÃ¡ntos jeans vendiste?"
Mensaje actual: "1"
â†’ VENTA, producto:"jean", cantidad:1, precio_unitario:40, total:40, moneda:PEN â€” registro completo.
"""

ONBOARDING_PROMPTS = {
    1: """Responde SOLO con JSON. Usuario nuevo en Boti, paso 1.
La "respuesta" debe ser bienvenida cÃ¡lida que: salude, explique en 2 lÃ­neas quÃ© hace Boti (ventas/gastos/inventario por WhatsApp), y pida el nombre del negocio.
{"intent":"ONBOARDING","datos":{"paso":1},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    2: """Responde SOLO con JSON. Paso 2 del onboarding.
La "respuesta" pregunta quÃ© tipo de ropa vende (dama, caballero, niÃ±os, todo).
{"intent":"ONBOARDING","datos":{"paso":2},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    3: """Responde SOLO con JSON. Paso 3 del onboarding.
La "respuesta" pregunta a quÃ© hora cierra la tienda para enviar resumen diario.
{"intent":"ONBOARDING","datos":{"paso":3},"respuesta":"...","requiere_confirmacion":false,"siguiente_paso":""}""",

    4: """Responde SOLO con JSON. Paso 4 del onboarding, todo configurado.
La "respuesta" celebra que estÃ¡ listo y muestra ejemplo: "VendÃ­ 2 blusas a S/35 cada una".
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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  CORE: procesar mensaje principal
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def procesar_mensaje(
        self,
        mensaje: str,
        historial: list[dict] = None,
        contexto_negocio: dict = None,
    ) -> dict:
        """
        Procesa el mensaje del comerciante usando el historial de corto plazo.

        historial: lista de dicts [{role: "user"|"assistant", content: str}, ...]
                   con los Ãºltimos N turnos. Se inserta entre el system prompt
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
        # system â†’ historial de turnos anteriores â†’ mensaje actual
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
            logger.warning(f"[Groq] JSON invÃ¡lido: {raw[:200]}")
            return {
                "intent": "DESCONOCIDO",
                "datos": {},
                "respuesta": "No entendÃ­ bien. Â¿Puedes decirme quÃ© vendiste o en quÃ© te ayudo? ðŸ˜Š",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }
        except Exception as e:
            logger.error(f"[Groq] Error procesando mensaje: {e}")
            return {
                "intent": "ERROR",
                "datos": {},
                "respuesta": "Ups, tuve un problema tÃ©cnico. Intenta de nuevo ðŸ™",
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  STOCK: resolver producto para descuento
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def resolver_producto_venta(
        self,
        nombre_extraido: str,
        candidatos: list[dict],
    ) -> dict:
        """
        Dado el nombre que el NLP extrajo y una lista de candidatos
        de la BD (ya filtrados por pg_trgm), decide cuÃ¡l es el match.

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
            f'{i+1}. "{c["nombre"]}"' + (f' (Talla: {c["talla"]})' if c.get("talla") else "")
            for i, c in enumerate(candidatos)
        )
        prompt = (
            f'El comerciante dijo que vendiÃ³: "{nombre_extraido}"\n\n'
            f"Estos son los productos registrados en su catÃ¡logo:\n{lista_txt}\n\n"
            f"Â¿Alguno de estos productos es claramente el mismo que mencionÃ³ el comerciante?\n"
            f"Responde SOLO con JSON:\n"
            f'{{"match": "exacto"|"parcial"|"ninguno", "indice": 1..N|null}}\n'
            f"- exacto: hay UNO que claramente es el mismo producto.\n"
            f"- parcial: hay varios que podrÃ­an serlo, no se puede decidir solo.\n"
            f"- ninguno: ninguno coincide con lo que dijo el comerciante.\n"
            f"indice: nÃºmero del producto si match=exacto, null en cualquier otro caso."
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador de productos. Responde SOLO con JSON vÃ¡lido.",
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
        Genera el mensaje de confirmaciÃ³n para crear un producto nuevo
        que no estaba en el catÃ¡logo.
        """
        precio_txt = f" con precio S/{precio:.2f}" if precio else ""
        prompt = (
            f'Genera un mensaje corto de WhatsApp (mÃ¡ximo 2 lÃ­neas) para preguntarle '
            f'al comerciante si quiere agregar "{nombre}"{precio_txt} a su catÃ¡logo, '
            f"dado que acaba de vender {cantidad} unidad(es) y no estaba registrado. "
            f"Habla en espaÃ±ol peruano cÃ¡lido. Termina siempre la frase con: Â¿te interesa agregarlo (sÃ­/no)? Sin markdown."
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
                f'No tengo "{nombre}" en tu catÃ¡logo. '
                f"Â¿Lo agrego{precio_str} (sÃ­/no)?"
            )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  ONBOARDING
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def procesar_onboarding(self, paso: int, mensaje_usuario: str = "") -> dict:
        prompt = ONBOARDING_PROMPTS.get(paso, ONBOARDING_PROMPTS[1])
        if mensaje_usuario:
            prompt += f"\nEl usuario escribiÃ³: '{mensaje_usuario}'"

        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres Boti, asistente de negocios por WhatsApp para "
                            "comerciantes de ropa en Tacna, PerÃº. "
                            "Habla en espaÃ±ol peruano cÃ¡lido y natural."
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
                1: "Â¡Hola! Soy Boti ðŸ‘‹ Te ayudo a controlar ventas y gastos de tu negocio por WhatsApp. Para empezar, Â¿cÃ³mo se llama tu negocio?",
                2: "Â¿QuÃ© tipo de ropa vendes principalmente? (dama, caballero, niÃ±os, todo)",
                3: "Â¿A quÃ© hora cierras tu tienda? Te mando el resumen del dÃ­a a esa hora ðŸ“Š",
                4: "Â¡Todo listo! ðŸŽ‰ Prueba escribiendo: 'VendÃ­ 2 polos a S/25 cada uno'",
            }
            return {
                "intent": "ONBOARDING",
                "datos": {"paso": paso},
                "respuesta": fallbacks.get(paso, "Â¡Hola! Â¿CÃ³mo se llama tu negocio?"),
                "requiere_confirmacion": False,
                "siguiente_paso": "",
            }

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  REPORTES
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def generar_resumen_reporte(self, datos: dict) -> str:
        prompt = f"""Eres Boti. Genera un resumen de reporte para WhatsApp con estos datos:
{json.dumps(datos, ensure_ascii=False)}

Reglas:
- MÃ¡ximo 5 lÃ­neas
- Emojis (ðŸ’°ðŸ“¦ðŸ“Šâœ…)
- Mostrar ventas, gastos y ganancia neta en soles
- EspaÃ±ol peruano natural
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
                f"ðŸ“Š Reporte de {datos.get('periodo', 'hoy')}:\n"
                f"ðŸ’° Ventas: S/{tv:.2f}\n"
                f"ðŸ“¦ Gastos: S/{tg:.2f}\n"
                f"âœ… Ganancia: S/{tv - tg:.2f}"
            )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  EDICIÃ“N DE TRANSACCIONES
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def interpretar_edicion(self, transaccion: dict, mensaje: str) -> dict:
        prompt = f"""Eres Boti. Un usuario quiere editar una transacciÃ³n existente.
TransacciÃ³n actual:
{json.dumps(transaccion, ensure_ascii=False)}

El usuario ha dicho: "{mensaje}"

Devuelve SOLO un JSON con los campos de la transacciÃ³n que deben actualizarse.
Posibles campos: "descripcion", "monto", "moneda", "tipo".
Ejemplo: si dice "cambia el monto a 50", devuelve {{"monto": 50}}.
Si dice "era en dolares", devuelve {{"moneda": "USD"}}.
Si dice "la descripcion era polos", devuelve {{"descripcion": "polos"}}.
Si no entiendes quÃ© cambiar, devuelve un JSON vacÃ­o {{}}.
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
            logger.error(f"[Groq] Error interpretando ediciÃ³n: {e}")
            return {}

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  ONBOARDING: extractores de datos
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def extraer_dato(self, campo: str, mensaje: str) -> str:
        prompt = (
            f'El usuario escribiÃ³: "{mensaje}"\n\n'
            f"Extrae Ãºnicamente el valor correspondiente al campo: {campo}.\n"
            f"Responde SOLO con el valor extraÃ­do, sin explicaciones ni puntuaciÃ³n extra.\n"
            f"Si no puedes identificarlo con claridad, devuelve el texto tal como estÃ¡, limpio."
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
            logger.info(f"[Groq] extraer_dato campo='{campo}' â†’ '{resultado}'")
            return resultado if resultado else mensaje.strip()
        except Exception as e:
            logger.error(f"[Groq] extraer_dato error: {e}")
            return mensaje.strip()

    async def extraer_monedas(self, mensaje: str) -> str:
        prompt = (
            f'El usuario respondiÃ³ sobre quÃ© monedas acepta en su negocio: "{mensaje}"\n\n'
            f"Clasifica su respuesta y responde ÃšNICAMENTE con uno de estos cÃ³digos:\n"
            f"PEN      â†’ solo acepta soles peruanos\n"
            f"CLP      â†’ solo acepta pesos chilenos\n"
            f"PEN,CLP  â†’ acepta ambas monedas\n\n"
            f"Responde solo el cÃ³digo, sin explicaciones."
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
                logger.info(f"[Groq] extraer_monedas â†’ '{resultado}'")
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
            f'Un comerciante en Tacna, PerÃº vende: "{tipo_ropa}".\n\n'
            f"Genera exactamente 5 categorÃ­as de inventario para ese tipo de negocio.\n"
            f"Reglas:\n"
            f"- Nombres cortos (1-3 palabras), en espaÃ±ol, con mayÃºscula inicial.\n"
            f"- Responde ÃšNICAMENTE con un array JSON vÃ¡lido.\n"
            f"- Sin texto antes ni despuÃ©s, sin bloques de cÃ³digo markdown.\n\n"
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
                            "Eres un asistente que responde SOLO con arrays JSON vÃ¡lidos. "
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
                logger.info(f"[Groq] generar_categorias tipo='{tipo_ropa}' â†’ {categorias}")
                return {"categorias": categorias}
            return {"categorias": fallback}
        except json.JSONDecodeError as e:
            logger.error(f"[Groq] generar_categorias JSON invÃ¡lido: {e}")
            return {"categorias": fallback}
        except Exception as e:
            logger.error(f"[Groq] generar_categorias error: {e}")
            return {"categorias": fallback}
    
    async def extraer_producto_inventario(self, mensaje: str) -> dict:
        """
        Dado un mensaje en lenguaje libre, extrae los datos de un producto
        para carga de inventario durante el onboarding.
        Retorna dict con claves: nombre, talla, precio_venta, precio_compra, cantidad
        (los que no se mencionen llegan como None).
        """
        prompt = (
            f'Un comerciante de ropa en Tacna escribió esto para registrar un producto:\n'
            f'"{mensaje}"\n\n'
            f'Extrae los datos y responde SOLO con JSON válido, sin texto adicional:\n'
            f'{{\n'
            f'  "nombre": "nombre del producto capitalizado, ej: Polo Básico",\n'
            f'  "talla": "talla en mayúsculas, ej: M, XL, 28, TALLA ÚNICA",\n'
            f'  "cantidad": número entero o null,\n'
            f'  "precio_venta": número decimal o null,\n'
            f'  "precio_compra": número decimal o null\n'
            f'}}\n\n'
            f'Reglas:\n'
            f'- nombre: capitaliza cada palabra. Si no se menciona → null.\n'
            f'- talla: siempre en MAYÚSCULAS. Si no se menciona → null.\n'
            f'- cantidad: solo el número entero. (stock) Si no se menciona → null.\n'
            f'- precio_venta: solo el número. Si no se menciona → null.\n'
            f'- precio_compra: solo el número (precio de costo). Si no se menciona → null.\n'
            f'Ejemplos:\n'
            f'"Polo manga corta, Talla XL, stock 50, precio de venta 50, precio de compra 30" → {{"nombre":"Polo Manga Corta","talla":"XL","cantidad":50,"precio_venta":50.0,"precio_compra":30.0}}\n'
            f'"Jean slim 28 25 soles 8 unidades" → {{"nombre":"Jean Slim","talla":"28","cantidad":8,"precio_venta":25.0,"precio_compra":null}}'
        )
        fallback = {"nombre": None, "talla": None, "cantidad": None, "precio_venta": None, "precio_compra": None}
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres un extractor de datos de productos de ropa. "
                            "Responde SOLO con JSON vÃ¡lido, sin texto adicional ni markdown."
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

            logger.info(f"[Groq] extraer_producto_inventario â†’ {resultado}")
            return resultado

        except json.JSONDecodeError as e:
            logger.error(f"[Groq] extraer_producto_inventario JSON invÃ¡lido: {e}")
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
        Dado el nombre de un producto y las categorÃ­as existentes del negocio,
        intenta hacer match semÃ¡ntico y devuelve la categorÃ­a sugerida o None.
        
        Retorna:
        {
        "match": true | false,
        "categoria": "nombre de la categorÃ­a" | null
        }
        """
        if not categorias_negocio:
            return {"match": False, "categoria": None}

        lista = ", ".join(f'"{c}"' for c in categorias_negocio)
        prompt = (
            f'Producto: "{nombre_producto}"\n'
            f'CategorÃ­as disponibles: [{lista}]\n\n'
            f'Â¿A cuÃ¡l de esas categorÃ­as pertenece este producto?\n'
            f'Responde SOLO con JSON vÃ¡lido:\n'
            f'{{"match": true|false, "categoria": "nombre exacto de la categorÃ­a o null"}}\n\n'
            f'Reglas:\n'
            f'- match true solo si hay una categorÃ­a claramente correcta.\n'
            f'- categoria debe ser el nombre EXACTO de una de la lista, o null si match=false.\n'
            f'- Ejemplos: "Polo Urbanno" â†’ "Polos"; "Jean Slim" â†’ "Jeans"; "Chompa lana" â†’ "Chompas"\n'
            f'- Si no hay ninguna que aplique claramente â†’ match: false, categoria: null'
        )
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador de productos de ropa. Responde SOLO con JSON vÃ¡lido.",
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
            logger.info(f"[Groq] sugerir_categoria '{nombre_producto}' â†’ {resultado}")
            return resultado
        except Exception as e:
            logger.error(f"[Groq] sugerir_categoria error: {e}")
            return {"match": False, "categoria": None}

    async def interpretar_accion_categoria(
        self,
        mensaje: str,
        categorias_actuales: list[str],
    ) -> dict:
        """
        Mini-prompt EXCLUSIVO para el paso de edición de categorías en onboarding.

        Detecta la intención del usuario frente a la lista de categorías propuestas:
          - CONFIRMAR   → el usuario acepta la lista tal como está
          - AGREGAR     → quiere añadir una nueva categoría (extrae el nombre)
          - QUITAR      → quiere eliminar una categoría (extrae el número o nombre)
          - PRODUCTO    → se confundió y describió un producto específico, no una categoría
          - DESCONOCIDO → no se pudo interpretar

        Retorna un dict:
        {
          "accion": "CONFIRMAR" | "AGREGAR" | "QUITAR" | "PRODUCTO" | "DESCONOCIDO",
          "valor": str | int | None,
          "confianza": "alta" | "baja"
        }
        """
        lista_txt = "\n".join(f"{i+1}. {c}" for i, c in enumerate(categorias_actuales))

        prompt = f"""Estás ayudando a un comerciante a definir las CATEGORÍAS de su tienda de ropa.
Las categorías son nombres generales para clasificar productos (ej: Polos, Jeans, Blusas).

Categorías actuales propuestas:
{lista_txt}

El comerciante respondió: "{mensaje}"

Clasifica su intención en UNA de estas acciones:

CONFIRMAR → acepta la lista tal como está. Señales: "listo", "ok", "está bien", "sí", "así", "dale", "perfecto", "queda así", "me parece bien", "ya", "bueno", etc.

AGREGAR → quiere añadir una categoría nueva (nombre genérico de prenda, NO un producto específico).
  valor: el nombre de la categoría a agregar (solo nombre de prenda, ej: "Shorts", "Blusas")

QUITAR → quiere eliminar una categoría de la lista.
  valor: el número de la categoría (entero) o el nombre exacto de la categoría a quitar.

PRODUCTO → el usuario describe un producto específico con detalles como talla, color, modelo, etc.
  Ejemplos: "quiero agregar Blusas con ojuelas talla S y M", "agregar jean slim azul talla 28"
  Esto NO es una categoría, es un producto concreto.

DESCONOCIDO → no se puede determinar la intención.

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{"accion": "CONFIRMAR|AGREGAR|QUITAR|PRODUCTO|DESCONOCIDO", "valor": null, "confianza": "alta|baja"}}

REGLAS:
- Si solo dice el nombre de una prenda genérica sin detalles → AGREGAR
- Si da detalles específicos (talla concreta, precio, color específico, modelo) → PRODUCTO
- Si menciona un número referenciando la lista → QUITAR con ese número como valor
- confianza "alta" si la intención es clara, "baja" si hay ambigüedad
"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres un clasificador de intenciones para un chatbot de negocios. "
                            "Responde SOLO con JSON válido, sin markdown ni texto adicional."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=80,
            )
            raw = response.choices[0].message.content.strip()
            resultado = self._parsear(raw)
            logger.info(f"[Groq] interpretar_accion_categoria → {resultado}")
            accion = resultado.get("accion", "DESCONOCIDO").upper()
            valor = resultado.get("valor")
            if accion == "QUITAR" and valor is not None:
                try:
                    valor = int(str(valor).strip())
                except (ValueError, TypeError):
                    pass
            return {
                "accion": accion,
                "valor": valor,
                "confianza": resultado.get("confianza", "baja"),
            }
        except Exception as e:
            logger.error(f"[Groq] interpretar_accion_categoria error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["listo", "ok", "bien", "perfecto", "dale", "sí", "si", "ya"]):
                return {"accion": "CONFIRMAR", "valor": None, "confianza": "baja"}
            if any(w in msg for w in ["agregar", "añadir", "agrega", "añade"]):
                partes = msg.split(maxsplit=1)
                return {"accion": "AGREGAR", "valor": partes[1].strip().title() if len(partes) > 1 else None, "confianza": "baja"}
            if any(w in msg for w in ["quitar", "eliminar", "borrar", "quita"]):
                nums = re.findall(r"\d+", msg)
                return {"accion": "QUITAR", "valor": int(nums[0]) if nums else None, "confianza": "baja"}
            return {"accion": "DESCONOCIDO", "valor": None, "confianza": "baja"}
    async def interpretar_accion_inventario(self, mensaje: str) -> dict:
        """
        Mini-prompt para interpretar la respuesta del usuario tras confirmar un producto en onboarding.
        Retorna la acción: 'TERMINAR', 'AGREGAR_OTRO', 'CORREGIR', 'DESCONOCIDO'
        """
        prompt = f"""El bot acaba de confirmar un producto cargado al inventario.
El usuario responde: "{mensaje}"

Clasifica su intención en UNA de estas acciones:
TERMINAR → Señales: "todo bien", "sí", "queda", "listo", "está bien", "ok", "ya", "terminé", "siguiente fase", "pasar a la siguiente".
AGREGAR_OTRO → Señales: "otro", "quiero agregar otro", "más", "uno más", "agrega otro", "sí, otro", "agregar esto".
CORREGIR → Señales: "editar", "mal", "error", "no", "corregir", "cambiar".
DESCONOCIDO → No se puede determinar.

Responde ÚNICAMENTE con JSON válido:
{{"accion": "TERMINAR|AGREGAR_OTRO|CORREGIR|DESCONOCIDO"}}"""
        try:
            response = await client.chat.completions.create(
                model=MODELO_NLP,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un clasificador de intenciones. Responde SOLO con JSON válido.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=32,
            )
            raw = response.choices[0].message.content.strip()
            resultado = self._parsear(raw)
            return {"accion": resultado.get("accion", "DESCONOCIDO").upper()}
        except Exception as e:
            logger.error(f"[Groq] interpretar_accion_inventario error: {e}")
            msg = mensaje.strip().lower()
            if any(w in msg for w in ["queda", "listo", "bien", "ok", "ya"]): return {"accion": "TERMINAR"}
            if any(w in msg for w in ["otro", "mas", "más", "agrega"]): return {"accion": "AGREGAR_OTRO"}
            if any(w in msg for w in ["mal", "error", "no", "corregir", "cambiar"]): return {"accion": "CORREGIR"}
            return {"accion": "DESCONOCIDO"}


gemini_service = GeminiService()