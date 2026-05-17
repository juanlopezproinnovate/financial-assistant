"""
app/graph/nodes/producto_guiado.py

Lógica compartida para agregar productos guiado paso a paso.
Usada tanto en el flujo activo (negocio.py) como en el onboarding.

v2 - Flujo inteligente:
  · Extrae todos los campos posibles del primer mensaje (nombre, talla, precio, stock, precio_compra)
  · Normaliza: nombre en Title Case, talla en MAYÚSCULAS, "Única" si no viene talla
  · Asigna categoría automáticamente desde las del negocio; si no hay match la crea
  · Solo pregunta lo que realmente falta (precio_compra es siempre opcional, no se pregunta)
  · Mensaje de confirmación incluye: nombre, talla, stock, precio venta, precio compra, categoría
  · Flujo de edición: acepta cambios libres y re-muestra el resumen actualizado
"""
from app.services.gemini_service import gemini_service
from app.services.stock_service import stock_service
from app.database import get_pool
import logging
import re

logger = logging.getLogger(__name__)

# Tallas estándar que se reconocen como letras
TALLAS_LETRA = {"XS", "S", "M", "L", "XL", "XXL", "XXXL", "XL", "2XL", "3XL"}


def _normalizar_nombre(nombre: str) -> str:
    """Title-case inteligente: corrige mayúsculas y acentos básicos."""
    if not nombre:
        return nombre
    return nombre.strip().title()


def _normalizar_talla(talla: str | None) -> str:
    """
    Normaliza la talla:
    - Si es None o vacío → "Única"
    - Si es letra → MAYÚSCULAS (S, M, L, XL...)
    - Si es número → tal cual (28, 30, 32...)
    - Si dice 'unica', 'única', 'talla unica' → "Única"
    """
    if not talla:
        return "Única"
    t = talla.strip()
    t_lower = t.lower()
    if any(x in t_lower for x in ["única", "unica", "talla única", "talla unica", "tallaúnica"]):
        return "Única"
    # Si es número puro (talla de jean: 28, 30, 32...)
    if re.match(r"^\d+$", t):
        return t
    # Si es letra/código → uppercase
    return t.upper()


def _mensaje_confirmacion(formulario: dict) -> str:
    """
    Arma el mensaje de confirmación completo con todos los campos,
    incluyendo categoría asignada.
    """
    nombre   = formulario.get("nombre", "—")
    talla    = formulario.get("talla", "Única")
    stock    = formulario.get("cantidad", 0)
    pv       = formulario.get("precio_venta", 0.0)
    pc       = formulario.get("precio_compra")
    cat      = formulario.get("categoria", None)

    pc_txt  = f"S/ {pc:.2f}" if pc else "No asignado"
    cat_txt = cat if cat else "Sin categoría"

    return (
        f"✅ Esto es lo que registraré:\n\n"
        f"📝 Producto: *{nombre}*\n"
        f"📐 Talla: {talla}\n"
        f"📦 Stock: {stock} unidades\n"
        f"💰 Precio venta: S/ {pv:.2f}\n"
        f"💵 Precio compra: {pc_txt}\n"
        f"📂 Categoría: *{cat_txt}*\n\n"
        f"¿Quieres *guardar*, *editar* algo o *agregar otro* producto? 😊"
    )


def _extraer_nombre_desde_edicion(mensaje: str) -> str | None:
    """
    Extrae el nombre nuevo del producto de CUALQUIER frase de edición.
    Estrategia: detectar un verbo de comando + buscar el separador (por/a/como)
    y tomar TODO lo que viene después como el nombre.
    Funciona con:
      - "edita el nombre del producto por Polo con Cuello"
      - "cambia el nombre a Jean Slim Tela"
      - "el nombre del producto es Blusa Floral"
      - "el nombre es Polo Básico editalo"
    """
    msg = mensaje.strip()

    # ── Capa 1: verbo de edición + separador "por/a/como" al final ──
    # Detectar si hay un verbo de comando de edición
    tiene_verbo_edicion = bool(re.search(
        r"\b(?:edita(?:lo)?|cambia(?:lo)?|modifica|actualiza|pon(?:lo)?|cambiar|editar)\b",
        msg, re.IGNORECASE
    ))

    if tiene_verbo_edicion:
        # Buscar el ÚLTIMO "por", "a" o "como" que actúe como separador
        # y tomar todo lo que viene después como el nombre
        m = re.search(
            r"\b(?:por|a|como)\s+([A-Za-záéíóúÁÉÍÓÚñÑ][A-Za-záéíóúÁÉÍÓÚñÑ\s\-]+)$",
            msg, re.IGNORECASE
        )
        if m:
            nombre = m.group(1).strip()
            # Quitar palabras finales de comando que se hayan colado
            for sw in ["editalo", "edítalo", "guarda", "guardar", "listo", "ok"]:
                if nombre.lower().endswith(sw):
                    nombre = nombre[:-(len(sw))].strip()
            if len(nombre) >= 2:
                return nombre.strip()

    # ── Capa 2: patrones "el nombre es X" / "nombre: X" ──
    m2 = re.search(
        r"(?:el\s+)?nombre\s+(?:del\s+producto\s+)?(?:es|a|:)\s+"
        r"([A-Za-záéíóúÁÉÍÓÚñÑ][A-Za-záéíóúÁÉÍÓÚñÑ\s\-]+?)(?:\s+(?:editalo|edítalo|listo|guardar|ok))?$",
        msg, re.IGNORECASE
    )
    if m2:
        nombre = m2.group(1).strip()
        if len(nombre) >= 2:
            return nombre.strip()

    return None


def _extraer_campos_iniciales_regex(mensaje: str) -> dict:
    """
    Extractor regex robusto para el PRIMER mensaje del usuario donde manda
    todos los datos de un producto juntos.
    Ej: "Jean slim talla 28, precio 45 soles, stock 20"
    Ej: "polo basico talla M precio 35 stock 50 precio de compra 20"
    """
    msg = mensaje.strip()
    resultado: dict = {}

    # ── PRECIO COMPRA (detectar ANTES para no confundirlo con precio venta) ──
    m_pc = re.search(
        r"(?:precio\s+de\s+compra|me\s+cost[oó]|compr[eé]\s+a|lo\s+compr[eé]\s+a|me\s+sali[oó])\s*:?\s*(?:s/\s*)?(\d+(?:[.,]\d+)?)",
        msg, re.IGNORECASE
    )
    if m_pc:
        try:
            resultado["precio_compra"] = float(m_pc.group(1).replace(",", "."))
        except ValueError:
            pass

    # ── PRECIO VENTA ──
    m_pv = re.search(
        r"(?:precio(?:\s+de\s+venta)?|cuesta|vale|a\s+s/|vendo\s+a|sale\s+a)\s*:?\s*(?:s/\s*)?(\d+(?:[.,]\d+)?)",
        msg, re.IGNORECASE
    )
    if m_pv:
        # Verificar que no sea el precio de compra detectado antes
        if not m_pc or abs(m_pv.start() - m_pc.start()) > 5:
            try:
                resultado["precio_venta"] = float(m_pv.group(1).replace(",", "."))
            except ValueError:
                pass

    # Si solo hay un número suelto y no detectamos precio aún, asumirlo como precio_venta
    if "precio_venta" not in resultado and "precio_compra" not in resultado:
        m_solo = re.search(r"(?:a|por|en)\s+(?:s/\s*)?(\d+(?:[.,]\d+)?)\s*(?:soles?|sol)?", msg, re.IGNORECASE)
        if m_solo:
            try:
                resultado["precio_venta"] = float(m_solo.group(1).replace(",", "."))
            except ValueError:
                pass

    # ── STOCK / CANTIDAD ──
    m_stock = re.search(
        r"(?:stock|cantidad|unidades?|tengo|hay|me\s+quedan?)\s*:?\s*(?:de\s+)?(\d+)",
        msg, re.IGNORECASE
    )
    if m_stock:
        try:
            resultado["cantidad"] = int(m_stock.group(1))
        except ValueError:
            pass

    # ── TALLA ──
    m_talla = re.search(
        r"talla\s*:?\s*([A-Za-z]{1,4}|\d{2,3})\b",
        msg, re.IGNORECASE
    )
    if m_talla:
        resultado["talla"] = m_talla.group(1)
    else:
        # Detectar talla sola (S, M, L, XL, XXL, 28, 30, etc.) como palabra aislada
        m_talla2 = re.search(
            r"\b(XS|S|M|L|XL|XXL|XXXL|2XL|3XL|\d{2,3})\b",
            msg, re.IGNORECASE
        )
        if m_talla2:
            resultado["talla"] = m_talla2.group(1)

    # ── NOMBRE ──
    # Extraer todo lo que viene ANTES de las palabras clave de datos
    corte_pattern = re.search(
        r"\b(?:talla|precio|stock|cantidad|cuesta|vale|tengo|unidades?|me\s+cost[oó])\b",
        msg, re.IGNORECASE
    )
    if corte_pattern:
        nombre_raw = msg[:corte_pattern.start()].strip().rstrip(",;-")
        # Limpiar prefijos de ejemplo como "Ej:", "(Ej:", "Ejemplo:"
        nombre_raw = re.sub(r"^\(?[Ee]j(?:emplo)?[.:]?\s*", "", nombre_raw).strip()
        # Quitar guiones bajos y paréntesis iniciales
        nombre_raw = nombre_raw.lstrip("(_").rstrip(")_")
        if nombre_raw and len(nombre_raw) >= 2:
            resultado["nombre"] = nombre_raw.strip()
    elif not any(resultado.values()):
        # Si no detectamos nada más, el mensaje completo es el nombre
        resultado["nombre"] = msg.strip()

    return resultado


def _extraer_campos_por_regex(mensaje: str) -> dict:
    """
    Extrae campos de producto a partir de frases de edición explícitas.
    Cubre patrones como:
      - "edita la talla a 40"      → talla=40
      - "cambia el precio a 35"    → precio_venta=35
      - "el stock es 15"           → cantidad=15
      - "precio de compra 20"      → precio_compra=20
      - "nombre: Polo slim"        → nombre="Polo slim"
    Retorna un dict con solo los campos detectados (el resto es None).
    """
    msg = mensaje.strip()
    resultado: dict = {}

    # ── TALLA ──
    # Patrones: "talla a 40", "talla: XL", "talla es M", "cambia la talla a S"
    m = re.search(
        r"talla\s*(?:a|:|es|=)?\s*([A-Za-z]{1,4}|\d{2,3})\b",
        msg, re.IGNORECASE
    )
    if m:
        resultado["talla"] = m.group(1)

    # ── PRECIO VENTA ──
    # "precio (de venta)? (a|es|:)? 45 (soles)?"
    m = re.search(
        r"precio(?:\s+de\s+venta)?\s*(?:a|:|es|=)?\s*(?:s/\s*)?([\d]+(?:[.,]\d+)?)",
        msg, re.IGNORECASE
    )
    if m and "compra" not in msg[max(0, m.start()-10):m.start()].lower():
        try:
            resultado["precio_venta"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # ── PRECIO COMPRA ──
    m = re.search(
        r"precio\s+de\s+compra\s*(?:a|:|es|=)?\s*(?:s/\s*)?([\d]+(?:[.,]\d+)?)",
        msg, re.IGNORECASE
    )
    if m:
        try:
            resultado["precio_compra"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # ── STOCK / CANTIDAD ──
    # "stock a 50", "stock es 20", "cantidad: 10", "tengo 30 unidades"
    m = re.search(
        r"(?:stock|cantidad|unidades?)\s*(?:a|:|es|=|son)?\s*(\d+)",
        msg, re.IGNORECASE
    )
    if m:
        try:
            resultado["cantidad"] = int(m.group(1))
        except ValueError:
            pass

    # ── NOMBRE ──
    # "nombre: Polo básico", "nombre es Blusa floral", "cambia el nombre a Jean slim"
    m = re.search(
        r"nombre\s*(?:a|:|es|=)?\s*([A-Za-záéíóúÁÉÍÓÚñÑ][A-Za-záéíóúÁÉÍÓÚñÑ\s]{2,40}?)(?:\s*[,.]|$)",
        msg, re.IGNORECASE
    )
    if m:
        resultado["nombre"] = m.group(1).strip()

    return resultado


async def _asignar_categoria(negocio_id: str, nombre_producto: str) -> str | None:
    """
    Busca la categoría más adecuada para el producto entre las del negocio.
    Si no hay match, crea la categoría automáticamente.
    Retorna el nombre de la categoría asignada.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, nombre FROM categorias WHERE negocio_id = $1 AND activa = true",
            negocio_id,
        )

    if not rows:
        return None

    categorias = [{"id": str(r["id"]), "nombre": r["nombre"]} for r in rows]
    nombres_cats = [c["nombre"] for c in categorias]

    # Pedir al LLM que sugiera categoría
    sugerencia = await gemini_service.sugerir_categoria_producto(nombre_producto, nombres_cats)

    if sugerencia.get("match") and sugerencia.get("categoria"):
        return sugerencia["categoria"]

    # No hubo match → intentar inferir la categoría desde el nombre del producto
    nombre_lower = nombre_producto.lower()
    categoria_inferida = None

    # Mapeo básico de palabras clave a categorías genéricas
    if any(w in nombre_lower for w in ["jean", "jeans", "pantalón", "pantalon", "pant"]):
        categoria_inferida = "Jeans"
    elif any(w in nombre_lower for w in ["polo", "polera", "camiseta", "tshirt"]):
        categoria_inferida = "Polos"
    elif any(w in nombre_lower for w in ["blusa", "blus"]):
        categoria_inferida = "Blusas"
    elif any(w in nombre_lower for w in ["short", "bermuda"]):
        categoria_inferida = "Shorts"
    elif any(w in nombre_lower for w in ["vestido", "dress"]):
        categoria_inferida = "Vestidos"
    elif any(w in nombre_lower for w in ["casaca", "chaqueta", "jacket", "abrigo"]):
        categoria_inferida = "Casacas"
    elif any(w in nombre_lower for w in ["chompa", "suéter", "sweater", "hoodie"]):
        categoria_inferida = "Chompas"
    elif any(w in nombre_lower for w in ["falda", "minifald"]):
        categoria_inferida = "Faldas"

    if not categoria_inferida:
        # Crear categoría genérica basada en el nombre
        categoria_inferida = nombre_producto.split()[0].title() + "s"

    # Crear la nueva categoría en BD
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO categorias (negocio_id, nombre, tipo, activa)
                VALUES ($1, $2, 'inventario', true)
                ON CONFLICT DO NOTHING
                RETURNING nombre
                """,
                negocio_id,
                categoria_inferida,
            )
        logger.info(f"[producto_guiado] Categoría creada automáticamente: {categoria_inferida}")
    except Exception as e:
        logger.warning(f"[producto_guiado] No se pudo crear categoría: {e}")

    return categoria_inferida


async def agregar_producto_guiado(
    negocio_id: str,
    mensaje: str,
    datos: dict,
) -> dict:
    """
    Retorna:
    {
        "respuesta": str,
        "finalizado": bool,
        "datos": dict,
        "agregar_otro": bool,
    }
    """
    formulario = datos.get("formulario_producto", {})
    msg_lower  = mensaje.strip().lower()

    if msg_lower in ("cancelar", "salir"):
        return {
            "respuesta": "Cancelado. ¿En qué más te ayudo? 😊",
            "finalizado": True,
            "datos": {},
            "agregar_otro": False,
        }

    # ── Si ya está en confirmación → interpretar acción ──
    if datos.get("inv_confirmado"):
        return await _handle_confirmacion(negocio_id, mensaje, msg_lower, formulario, datos)

    # ── Extraer todos los campos posibles del mensaje ──
    campos_llm = await gemini_service.extraer_producto_inventario(mensaje, formulario)
    campos_rx  = _extraer_campos_iniciales_regex(mensaje)

    # Merge: LLM tiene prioridad, regex cubre cuando el LLM falla silenciosamente
    campos = {**campos_rx, **{k: v for k, v in campos_llm.items() if v is not None}}

    # Aplicar al formulario
    if campos.get("nombre"):
        formulario["nombre"] = _normalizar_nombre(campos["nombre"])
    if campos.get("talla") is not None:
        formulario["talla"] = _normalizar_talla(campos["talla"])
    if campos.get("precio_venta") is not None:
        formulario["precio_venta"] = campos["precio_venta"]
    if campos.get("precio_compra") is not None:
        formulario["precio_compra"] = campos["precio_compra"]
    if campos.get("cantidad") is not None:
        formulario["cantidad"] = campos["cantidad"]

    def _datos_actualizados():
        return {**datos, "formulario_producto": formulario, "inv_confirmado": False}

    # ── Pedir solo lo que falta (precio_compra NO se pregunta — es opcional) ──

    if not formulario.get("nombre"):
        return {
            "respuesta": (
                "¿Cómo se llama el producto?\n"
                "_(Ej: Polo básico, Jean slim, Blusa floral)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    # Si no vino talla, asignar "Única" silenciosamente
    if "talla" not in formulario or not formulario.get("talla"):
        formulario["talla"] = "Única"

    if formulario.get("precio_venta") is None:
        nombre_txt = formulario["nombre"]
        talla_txt  = formulario["talla"]
        return {
            "respuesta": (
                f"*{nombre_txt}* — Talla *{talla_txt}* ✅\n\n"
                "💰 ¿Cuál es el *precio de venta* en Soles?\n"
                "_(Ej: 35, 49.90, 120)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    if formulario.get("cantidad") is None:
        nombre_txt = formulario["nombre"]
        pv_txt     = formulario["precio_venta"]
        return {
            "respuesta": (
                f"*{nombre_txt}* — S/ {pv_txt:.2f} ✅\n\n"
                "📦 ¿Cuántas unidades tienes en stock?\n"
                "_(Ej: 10, 25 — escribe 0 si no tienes aún)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    # ── Todos los campos obligatorios listos → asignar categoría y confirmar ──
    if not formulario.get("categoria"):
        categoria = await _asignar_categoria(negocio_id, formulario["nombre"])
        formulario["categoria"] = categoria

    return {
        "respuesta": _mensaje_confirmacion(formulario),
        "finalizado": False,
        "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
        "agregar_otro": False,
    }


async def _handle_confirmacion(
    negocio_id: str,
    mensaje: str,
    msg_lower: str,
    formulario: dict,
    datos: dict,
) -> dict:
    """Maneja el turno de confirmación: guardar, editar, agregar otro o cancelar."""

    # ── Cancelar ──
    if msg_lower in ("cancelar", "salir", "no"):
        return {
            "respuesta": "Cancelado. ¿En qué más te ayudo? 😊",
            "finalizado": True,
            "datos": {},
            "agregar_otro": False,
        }

    # ── Fast-path regex: guardar / agregar_otro (no depender del LLM para esto) ──
    _GUARDAR_WORDS = {"guardar", "si", "sí", "ok", "dale", "listo", "bien",
                      "confirmar", "perfecto", "correcto", "queda", "ya", "va"}
    _OTRO_WORDS   = {"agregar otro", "otro producto", "otro", "más", "mas", "siguiente"}

    if msg_lower.strip() in _GUARDAR_WORDS:
        return await _guardar_producto(negocio_id, formulario, datos)

    if any(msg_lower.strip() == w or msg_lower.strip().startswith(w) for w in _OTRO_WORDS):
        return await _guardar_producto(negocio_id, formulario, datos, agregar_otro=True)

    # ── Detectar renombre explícito antes del LLM: "el nombre es X", "editalo por X" ──
    nombre_extraido = _extraer_nombre_desde_edicion(mensaje)

    # ── Interpretar Intención y Cambios con LLM ──
    interpretacion = await gemini_service.interpretar_accion_inventario(mensaje, formulario)
    accion = interpretacion.get("accion", "DESCONOCIDO")
    campos_llm = interpretacion.get("cambios", {})

    # Si el LLM no extrajo nombre pero la regex sí, inyectarlo
    if nombre_extraido and not campos_llm.get("nombre"):
        campos_llm["nombre"] = nombre_extraido
        if accion == "DESCONOCIDO":
            accion = "EDITAR"

    if accion == "TERMINAR":
        return await _guardar_producto(negocio_id, formulario, datos)

    if accion == "AGREGAR_OTRO":
        return await _guardar_producto(negocio_id, formulario, datos, agregar_otro=True)

    # ── Editar: fallback a regex si faltó algo ──
    campos_regex = _extraer_campos_por_regex(mensaje)

    # Merge: regex tiene prioridad porque es más confiable para frases de edición
    campos = {**campos_llm, **{k: v for k, v in campos_regex.items() if v is not None}}
    hubo_cambio = False

    if campos.get("nombre"):
        formulario["nombre"] = _normalizar_nombre(campos["nombre"])
        hubo_cambio = True
        formulario.pop("categoria", None)

    if campos.get("talla") is not None:
        formulario["talla"] = _normalizar_talla(campos["talla"])
        hubo_cambio = True

    if campos.get("precio_venta") is not None:
        formulario["precio_venta"] = campos["precio_venta"]
        hubo_cambio = True

    if campos.get("precio_compra") is not None:
        formulario["precio_compra"] = campos["precio_compra"]
        hubo_cambio = True

    if campos.get("cantidad") is not None:
        formulario["cantidad"] = campos["cantidad"]
        hubo_cambio = True

    # Detectar edición de categoría explícita
    # Patrones: "categoría Shorts", "cambia la categoría a Shorts", "categoría: Jeans"
    match_cat = re.search(
        r"categor[íi]a[s]?\s*(?:[:=]?\s*|\s+(?:a|de|en|es|por)\s+)([A-Za-záéíóúÁÉÍÓÚñÑ][A-Za-záéíóúÁÉÍÓÚñÑ\s]*)",
        mensaje,
        re.IGNORECASE,
    )
    if match_cat:
        # Limpiar preposiciones/artículos que puedan haberse colado al inicio
        _PREPOSICIONES = {"a", "de", "en", "es", "por", "la", "el", "los", "las", "un", "una"}
        captura = match_cat.group(1).strip()
        palabras = captura.split()
        # Quitar palabras iniciales que sean preposición/artículo
        while palabras and palabras[0].lower() in _PREPOSICIONES:
            palabras.pop(0)
        cat_limpia = " ".join(palabras).strip().title()

        if not cat_limpia:
            cat_limpia = captura.title()

        # Intentar hacer fuzzy match con categorías existentes del negocio
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT nombre FROM categorias WHERE negocio_id = $1 AND activa = true",
                negocio_id,
            )
        cats_existentes = [r["nombre"] for r in rows]

        # 1) Match exacto (case-insensitive)
        cat_final = next(
            (c for c in cats_existentes if c.lower() == cat_limpia.lower()), None
        )

        # 2) Match parcial: alguna categoría contiene el texto o viceversa
        if not cat_final:
            cat_final = next(
                (c for c in cats_existentes
                 if cat_limpia.lower() in c.lower() or c.lower() in cat_limpia.lower()),
                None,
            )

        # 3) Sin match → usar el nombre limpio y crear la categoría
        if not cat_final:
            cat_final = cat_limpia
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO categorias (negocio_id, nombre, tipo, activa)
                        VALUES ($1, $2, 'inventario', true)
                        ON CONFLICT DO NOTHING
                        """,
                        negocio_id,
                        cat_final,
                    )
                logger.info(f"[producto_guiado] Categoría creada: {cat_final}")
            except Exception as e:
                logger.warning(f"[producto_guiado] No se pudo crear categoría: {e}")

        formulario["categoria"] = cat_final
        hubo_cambio = True
        logger.info(f"[producto_guiado] Categoría asignada manualmente: '{cat_final}' (captura='{captura}')")

    if hubo_cambio:
        # Si cambiaron nombre, re-asignar categoría automáticamente si no hay ya
        if not formulario.get("categoria"):
            categoria = await _asignar_categoria(negocio_id, formulario["nombre"])
            formulario["categoria"] = categoria

        return {
            "respuesta": (
                "✏️ Actualizado:\n\n"
                + _mensaje_confirmacion(formulario)
            ),
            "finalizado": False,
            "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
            "agregar_otro": False,
        }

    # No entendió
    return {
        "respuesta": (
            "No entendí bien 😅\n"
            "Escribe *guardar* para confirmar, dime qué quieres cambiar, "
            "o *agregar otro* producto."
        ),
        "finalizado": False,
        "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
        "agregar_otro": False,
    }


async def _guardar_producto(
    negocio_id: str,
    formulario: dict,
    datos: dict,
    agregar_otro: bool = False,
) -> dict:
    """Verifica duplicado, inserta en BD y retorna respuesta final."""

    # Verificar duplicado
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM productos
            WHERE negocio_id = $1
              AND LOWER(nombre) = LOWER($2)
              AND LOWER(COALESCE(talla, '')) = LOWER(COALESCE($3, ''))
              AND activo = true
            LIMIT 1
            """,
            negocio_id,
            formulario["nombre"].strip(),
            formulario.get("talla") or "",
        )
    if row:
        talla_txt = f" Talla {formulario['talla']}" if formulario.get("talla") else ""
        return {
            "respuesta": (
                f"⚠️ *{formulario['nombre']}{talla_txt}* ya está en tu catálogo.\n\n"
                "¿Quieres agregar otro producto? 😊"
            ),
            "finalizado": True,
            "datos": {},
            "agregar_otro": agregar_otro,
        }

    # Resolver categoria_id si viene con categoría
    categoria_id = None
    categoria_nombre = formulario.get("categoria")
    if categoria_nombre:
        async with pool.acquire() as conn:
            cat_row = await conn.fetchrow(
                """
                SELECT id FROM categorias
                WHERE negocio_id = $1 AND LOWER(nombre) = LOWER($2) AND activa = true
                LIMIT 1
                """,
                negocio_id,
                categoria_nombre,
            )
        if cat_row:
            categoria_id = str(cat_row["id"])

    try:
        producto_id_nuevo = await stock_service.crear_producto(
            negocio_id       = negocio_id,
            nombre           = formulario["nombre"],
            talla            = formulario.get("talla"),
            precio_venta     = formulario["precio_venta"],
            precio_costo     = formulario.get("precio_compra"),
            cantidad_inicial = formulario["cantidad"],
            categoria_id     = categoria_id,
        )
    except Exception as e:
        logger.error(f"[producto_guiado] Error al guardar: {e}")
        return {
            "respuesta": "⚠️ Hubo un problema al guardar. Intenta de nuevo o escribe 'cancelar'.",
            "finalizado": False,
            "datos": datos,
            "agregar_otro": False,
        }

    nombre    = formulario["nombre"]
    talla     = formulario.get("talla", "Única")
    pv        = formulario.get("precio_venta", 0.0)
    stock_txt = formulario.get("cantidad", 0)
    pc        = formulario.get("precio_compra")
    cat       = formulario.get("categoria", "Sin categoría")

    pc_txt = f"S/ {pc:.2f}" if pc else "No asignado"

    respuesta = (
        f"✅ *{nombre}* guardado en tu inventario 📦\n\n"
        f"📐 Talla: {talla}\n"
        f"📦 Stock: {stock_txt} unidades\n"
        f"💰 Precio venta: S/ {pv:.2f}\n"
        f"💵 Precio compra: {pc_txt}\n"
        f"📂 Categoría: *{cat}*\n"
    )

    # Conservar campos del flow padre (ej: venta_monto) pero limpiar los de onboarding guiado
    datos_padre = {k: v for k, v in datos.items() if k not in ["formulario_producto", "inv_confirmado", "inv_sub_paso"]}

    return {
        "respuesta": respuesta,
        "finalizado": True,
        "datos": datos_padre,
        "agregar_otro": agregar_otro,
        "producto_id_nuevo": producto_id_nuevo,
    }