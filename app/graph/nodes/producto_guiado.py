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
    campos = await gemini_service.extraer_producto_inventario(mensaje, formulario)

    # Merge inteligente: solo actualizar campos que el LLM detectó
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

    # ── Guardar ──
    quiere_guardar = any(w in msg_lower for w in [
        "guardar", "sí", "si", "ok", "dale", "listo", "bien", "confirmar",
        "perfecto", "correcto", "todo bien", "así está", "queda"
    ])

    if quiere_guardar:
        return await _guardar_producto(negocio_id, formulario, datos)

    # ── Agregar otro (sin guardar el actual primero no tiene sentido — guardamos y seguimos) ──
    quiere_otro = any(w in msg_lower for w in ["otro", "agregar otro", "más", "mas", "siguiente"])
    if quiere_otro:
        return await _guardar_producto(negocio_id, formulario, datos, agregar_otro=True)

    # ── Editar: primero regex explícito, luego LLM como fallback ──
    campos_regex = _extraer_campos_por_regex(mensaje)
    campos_llm   = await gemini_service.extraer_producto_inventario(mensaje, formulario)

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