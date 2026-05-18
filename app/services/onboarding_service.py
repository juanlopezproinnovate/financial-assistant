"""
app/services/onboarding_service.py

Flujo de onboarding completo:
  onboarding_1  → Bienvenida + capturar nombre del negocio
  onboarding_2  → Capturar nombre propio del dueño
  onboarding_3  → Capturar monedas aceptadas (PEN / CLP / ambas)
  onboarding_4  → Capturar tipo de ropa
  onboarding_5  → Proponer categorías y permitir ajustes hasta "listo"
  onboarding_5b → Preguntar si carga inventario ahora, después o desde dashboard
  onboarding_5c → Bucle de carga de productos (nombre, talla, precio, stock, etiqueta)
  onboarding_6  → Capturar horario de cierre → completar onboarding

Cambios v3:
  - Nuevo paso onboarding_5b: elegir cuándo cargar inventario
  - Nuevo paso onboarding_5c: bucle de carga producto a producto
    · Solicita nombre, talla (obligatoria), precio unitario, cantidad en stock, etiqueta (opcional)
    · Inserta en tablas `productos` y `stock`
    · Al finalizar cada producto pregunta si sigue o termina
  - _leer_historial(): lee los últimos N pares del campo datos_temporales
  - guardar_en_historial(): persiste el par (user, assistant) en datos_temporales
    sin pisar otros campos como candidatos_stock, etc.

Schema usado:
  negocios           : whatsapp_numero, nombre_negocio, nombre_propietario, rubro,
                       monedas_aceptadas, horario_cierre, estado, onboarding_completo
  sesiones           : negocio_id (FK), estado_conversacion, datos_temporales
  categorias         : negocio_id (FK), nombre, tipo, color, activa
  categorias_plantilla: tipo_ropa, categorias (jsonb array)
  productos          : negocio_id (FK), nombre, nombre_variantes, precio_venta_pen,
                       unidad, activo, categoria_id
  stock              : producto_id (FK, unique), cantidad_actual, cantidad_minima
"""

import json
import re
import uuid
import decimal
import logging
from app.database import get_pool
from app.services.gemini_service import gemini_service
from datetime import datetime, date

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Palabras clave que el cliente usa para confirmar que las categorías están ok
# ──────────────────────────────────────────────────────────────────────────────
PALABRAS_CONFIRMAR = {"listo", "ok", "bien", "perfecto", "dale", "sí", "si", "ya", "confirmar"}

# ──────────────────────────────────────────────────────────────────────────────
#  Sub-pasos dentro de onboarding_5c (carga de inventario)
# ──────────────────────────────────────────────────────────────────────────────
SUB_PASO_NOMBRE    = "nombre"
SUB_PASO_COMPLETO  = "completo"
SUB_PASO_TALLA     = "talla"
SUB_PASO_PRECIO    = "precio"
SUB_PASO_STOCK     = "stock"
SUB_PASO_ETIQUETA  = "etiqueta"
SUB_PASO_CONTINUAR = "continuar"
SUB_PASO_CONFIRMAR = "confirmar"

def _armar_mensaje_confirmacion(producto: dict, sugerencia: dict, categorias: list[str]) -> str:
    """Arma el mensaje de resumen + categoría + solicitud de confirmación."""
    compra_linea = f"🏷️ Precio compra: S/ {producto['precio_compra']:.2f}\n" if producto.get("precio_compra") else ""

    if sugerencia.get("match"):
        cat_linea = f"📂 Categoría: *{sugerencia['categoria']}* ✅\n\n"
        cat_instruccion = ""
        pregunta_final = "¿Quieres editar el producto, agregar otro producto o terminar el inventario? 😊"
    else:
        cat_linea = "📂 Categoría: _ninguna coincide_\n"
        cats_txt = ", ".join(categorias) if categorias else "—"
        cat_instruccion = (
            f"\n¿Quieres asignarle una categoría?\n"
            f"Categorías disponibles: _{cats_txt}_\n"
            f"Escribe *categoría [Nombre]* para asignarla o crear una nueva.\n"
            f"_(Yo me encargaré de poner la primera letra en mayúscula y corregir la ortografía)_ ✨\n\n"
        )
        pregunta_final = "¿O prefieres guardar el producto sin categoría? Escribe 'sí' para guardar, o 'corregir'."

    return (
        f"📝 Producto: *{producto['nombre']}*\n"
        f"📐 Talla: {producto['talla']}\n"
        f"📦 Stock: {producto['cantidad']} unidades\n"
        f"💰 Precio venta: S/ {producto['precio_venta']:.2f}\n"
        f"{compra_linea}"
        f"{cat_linea}"
        f"{cat_instruccion}"
        f"{pregunta_final}"
    )


class OnboardingService:

    # ──────────────────────────────────────────────
    #  QUERIES — tabla negocios / sesiones
    # ──────────────────────────────────────────────

    async def get_negocio(self, telefono: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM negocios WHERE whatsapp_numero = $1", telefono
            )
        return dict(row) if row else None

    async def _extraer_y_validar_horario(self, mensaje: str) -> str | None:
        """
        Extrae hora de cierre del mensaje.
        Retorna string "HH:MM" o None si no detecta un horario válido.
        """
        import re
        msg = mensaje.strip().lower()
        
        # Formato HH:MM o H:MM
        m = re.search(r'\b(\d{1,2}):(\d{2})\b', msg)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
        
        # Formato "8pm", "8 pm", "20h"
        m = re.search(r'\b(\d{1,2})\s*(pm|am|h)\b', msg)
        if m:
            h = int(m.group(1))
            sufijo = m.group(2)
            if sufijo == "pm" and h != 12:
                h += 12
            elif sufijo == "am" and h == 12:
                h = 0
            if 0 <= h <= 23:
                return f"{h:02d}:00"
        
        # Texto natural: "8 de la noche", "9 de la tarde"
        m = re.search(r'\b(\d{1,2})\s+de\s+la\s+(noche|tarde|mañana|madrugada)\b', msg)
        if m:
            h = int(m.group(1))
            periodo = m.group(2)
            if periodo in ("noche", "tarde") and h != 12:
                h += 12
            elif periodo == "mañana" and h == 12:
                h = 0
            if 0 <= h <= 23:
                return f"{h:02d}:00"
        
        # Solo número entre 1 y 23 → asumir PM si es <= 12
        m = re.search(r'^\s*(\d{1,2})\s*$', msg)
        if m:
            h = int(m.group(1))
            if 1 <= h <= 12:
                h += 12  # asumir PM
            if 0 <= h <= 23:
                return f"{h:02d}:00"
        
        return None  # no es un horario válido → pedir de nuevo

    async def get_sesion(self, telefono: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.*
                FROM sesiones s
                JOIN negocios n ON n.id = s.negocio_id
                WHERE n.whatsapp_numero = $1
                ORDER BY s.created_at DESC
                LIMIT 1
                """,
                telefono,
            )
        return dict(row) if row else None

    async def upsert_sesion(self, negocio_id: str, estado: str, datos_temp: dict = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sesiones (negocio_id, estado_conversacion, datos_temporales)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (negocio_id)
                DO UPDATE SET
                    estado_conversacion = $2,
                    datos_temporales    = $3::jsonb,
                    ultimo_mensaje_at   = NOW()
                """,
                negocio_id,
                estado,
                json.dumps(datos_temp or {}, default=self._json_serial),
            )

    async def crear_negocio(self, telefono: str) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO negocios (whatsapp_numero, estado, onboarding_completo)
                VALUES ($1, 'onboarding', false)
                ON CONFLICT (whatsapp_numero) DO UPDATE
                    SET updated_at = NOW()
                RETURNING id
                """,
                telefono,
            )
        return str(row["id"])

    async def actualizar_negocio(self, negocio_id: str, **campos) -> None:
        if not campos:
            return
        sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(campos))
        valores = list(campos.values())
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE negocios SET {sets}, updated_at = NOW() WHERE id = $1",
                negocio_id, *valores,
            )

    async def completar_onboarding(self, negocio_id: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE negocios
                SET onboarding_completo = true, estado = 'activo', updated_at = NOW()
                WHERE id = $1
                """,
                negocio_id,
            )

    async def _leer_categorias_negocio(self, negocio_id: str) -> list[str]:
        """Retorna lista de nombres de categorías activas del negocio."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT nombre FROM categorias WHERE negocio_id = $1 AND activa = true",
                negocio_id,
            )
        return [row["nombre"] for row in rows]

    # ──────────────────────────────────────────────
    #  QUERIES — categorias_plantilla (caché IA)
    # ──────────────────────────────────────────────

    async def buscar_plantilla_categorias(self, tipo_ropa_normalizado: str) -> list[str] | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT categorias FROM categorias_plantilla WHERE tipo_ropa = $1",
                tipo_ropa_normalizado,
            )
        if not row:
            return None
        raw = row["categorias"]
        if isinstance(raw, str):
            return json.loads(raw)
        return list(raw)

    async def guardar_plantilla_categorias(self, tipo_ropa_normalizado: str, categorias: list[str]) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO categorias_plantilla (tipo_ropa, categorias, generado_por_ia)
                VALUES ($1, $2::jsonb, true)
                ON CONFLICT (tipo_ropa)
                DO UPDATE SET
                    veces_usado = categorias_plantilla.veces_usado + 1,
                    updated_at  = NOW()
                """,
                tipo_ropa_normalizado,
                json.dumps(categorias),
            )

    async def guardar_categorias_negocio(self, negocio_id: str, categorias: list[str]) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM categorias WHERE negocio_id = $1", negocio_id
            )
            for nombre in categorias:
                await conn.execute(
                    """
                    INSERT INTO categorias (negocio_id, nombre, tipo, activa)
                    VALUES ($1, $2, 'inventario', true)
                    """,
                    negocio_id,
                    nombre.strip(),
                )

    # ──────────────────────────────────────────────
    #  QUERIES — productos + stock (onboarding_5c)
    # ──────────────────────────────────────────────

    async def _buscar_categoria_id(self, negocio_id: str, nombre_categoria: str) -> str | None:
        """Busca el UUID de una categoría por nombre para asociar el producto."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM categorias
                WHERE negocio_id = $1 AND LOWER(nombre) = LOWER($2) AND activa = true
                LIMIT 1
                """,
                negocio_id,
                nombre_categoria,
            )
        return str(row["id"]) if row else None

    async def insertar_producto_con_stock(
        self,
        negocio_id: str,
        nombre: str,
        talla: str,
        precio_venta: float,
        cantidad: int,
        precio_costo: float | None = None,
        etiqueta: str | None = None,
        categoria_nombre: str | None = None,
    ) -> str:
        """
        Inserta un producto en `productos` y crea su registro en `stock`.
        Retorna el producto_id creado.
        El campo nombre_variantes se usa para almacenar la talla.
        """
        pool = await get_pool()

        # Armar nombre_variantes: siempre incluye la talla, opcionalmente la etiqueta
        variantes = [talla]
        if etiqueta:
            variantes.append(etiqueta)

        # Resolver categoria_id si fue dado
        categoria_id = None
        if categoria_nombre:
            categoria_id = await self._buscar_categoria_id(negocio_id, categoria_nombre)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO productos
                    (negocio_id, nombre, talla, nombre_variantes, precio_venta_pen, precio_costo, unidad, activo, categoria_id)
                VALUES
                    ($1, $2, $3, $4, $5, $6, 'unidad', true, $7)
                RETURNING id
                """,
                negocio_id,
                nombre.strip(),
                talla,
                variantes,
                precio_venta,
                precio_costo,
                categoria_id,
            )
            producto_id = str(row["id"])

            await conn.execute(
                """
                INSERT INTO stock (producto_id, cantidad_actual, cantidad_minima)
                VALUES ($1, $2, 5)
                ON CONFLICT (producto_id) DO UPDATE
                    SET cantidad_actual       = $2,
                        ultima_actualizacion  = NOW()
                """,
                producto_id,
                cantidad,
            )

        logger.info(
            f"[Onboarding] Producto creado: '{nombre}' talla={talla} "
            f"precio={precio_venta} stock={cantidad} negocio={negocio_id}"
        )
        return producto_id

    # ──────────────────────────────────────────────
    #  HELPERS — datos temporales
    # ──────────────────────────────────────────────

    def _leer_datos_temp(self, sesion: dict | None) -> dict:
        if not sesion:
            return {}
        raw = sesion.get("datos_temporales", {})
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw or {}

    # ──────────────────────────────────────────────
    #  HELPERS — parseo de formulario completo
    # ──────────────────────────────────────────────
    def _parsear_formulario_producto(self, texto: str) -> dict:
        """
        Intenta extraer nombre, talla, precio, stock y etiqueta
        de un mensaje en formato libre o con etiquetas explícitas.
        Retorna un dict con los campos encontrados (puede estar incompleto).
        """
        import re

        resultado = {}
        lineas = [l.strip() for l in texto.strip().splitlines() if l.strip()]

        # Mapeo de etiquetas posibles → campo interno
        patrones = {
            "nombre":   r"(?:nombre|producto)\s*[:\-]\s*(.+)",
            "talla":    r"talla\s*[:\-]\s*(.+)",
            "precio":   r"precio\s*[:\-]\s*(.+)",
            "stock":    r"(?:stock|cantidad|unidades?)\s*[:\-]\s*(.+)",
            "etiqueta": r"etiqueta\s*[:\-]\s*(.+)",
        }

        for linea in lineas:
            linea_lower = linea.lower()
            for campo, patron in patrones.items():
                if campo in resultado:
                    continue
                m = re.search(patron, linea_lower)
                if m:
                    valor_raw = linea[m.start(1):m.start(1) + len(m.group(1))].strip()
                    resultado[campo] = valor_raw

        # Post-procesado de tipos
        if "precio" in resultado:
            precio = self._parsear_precio(resultado["precio"])
            resultado["precio"] = precio  # None si no se pudo parsear
        if "stock" in resultado:
            cantidad = self._parsear_cantidad(resultado["stock"])
            resultado["stock"] = cantidad  # None si no se pudo parsear
        if "etiqueta" in resultado:
            if self._es_omision(resultado["etiqueta"]):
                resultado["etiqueta"] = None

        return resultado

    def _json_serial(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        raise TypeError(f"Type {type(obj)} not serializable")

    def _mensaje_plantilla_producto(self, num_producto: int = 1) -> str:
        """
        Devuelve el mensaje con la plantilla de carga de producto,
        amigable para personas mayores.
        """
        es_primero = num_producto == 1
        intro = "¡Genial! Vamos a cargar tu inventario 📦" if es_primero else "¡Perfecto! Siguiente producto 📦"
        return (
            f"{intro}\n\n"
            "Para registrar tus productos, puedes usar el siguiente formato:\n\n"
            "📝 *Nombre:* Polo básico\n"
            "📐 *Talla:* M\n"
            "📦 *Stock:* 10\n"
            "💰 *Precio de Venta:* 35\n"
            "💵 *Precio de Compra:* 10 (opcional)\n\n"
            "💡 _Puedes copiar el formato de arriba y solo cambiar los datos, o enviar en un solo texto o audio:_\n\n"
            "_Polo manga corta, Talla XL, stock 50, precio de venta 50, precio de compra 30_\n\n"
            "Si no tienes toda la información ahora, no te preocupes, escribe lo que tengas y te ayudaré con el resto. 🚀"
        )

    # ──────────────────────────────────────────────
    #  HELPERS — historial de corto plazo
    # ──────────────────────────────────────────────

    def _leer_historial(self, sesion: dict | None) -> list[dict]:
        datos = self._leer_datos_temp(sesion)
        return datos.get("historial_mensajes", [])

    async def guardar_en_historial(
        self,
        negocio_id: str,
        sesion: dict | None,
        user_msg: str,
        bot_msg: str,
        max_turns: int = 3,
    ) -> None:
        latest_sesion = await self._obtener_sesion_bot(negocio_id)
        datos = self._leer_datos_temp(latest_sesion)
        historial = datos.get("historial_mensajes", [])

        historial.append({"role": "user",      "content": user_msg})
        historial.append({"role": "assistant",  "content": bot_msg})

        max_mensajes = max_turns * 2
        if len(historial) > max_mensajes:
            historial = historial[-max_mensajes:]

        datos["historial_mensajes"] = historial

        estado_actual = (
            latest_sesion.get("estado_conversacion", "activo") if latest_sesion else "activo"
        )
        await self.upsert_sesion(negocio_id, estado_actual, datos)

    # ──────────────────────────────────────────────
    #  HELPERS — otros
    # ──────────────────────────────────────────────

    async def _guardar_y_confirmar_producto(
        self,
        negocio_id: str,
        datos_temp: dict,
        producto_actual: dict,
        etiqueta: str | None,
        productos_count: int,
    ) -> str:
        """Inserta el producto en BD, actualiza contadores y devuelve mensaje de confirmación."""
        try:
            await self.insertar_producto_con_stock(
                negocio_id   = negocio_id,
                nombre       = producto_actual["nombre"],
                talla        = producto_actual["talla"],
                precio_venta = producto_actual["precio_venta"],
                cantidad     = producto_actual["cantidad"],
                etiqueta     = etiqueta,
            )
            productos_count += 1
            datos_temp["inv_productos_cargados"] = productos_count
        except Exception as e:
            logger.error(f"[Onboarding] Error al guardar producto: {e}")
            return (
                "⚠️ Hubo un problema al guardar el producto. "
                "Por favor escribe el nombre nuevamente para intentar de nuevo."
            )

        etiqueta_txt = f" · _{etiqueta}_" if etiqueta else ""
        resumen = (
            f"✅ *{producto_actual['nombre']}* guardado\n\n"
            f"📐 Talla: {producto_actual['talla']}\n"
            f"💰 Precio: S/ {producto_actual['precio_venta']:.2f}\n"
            f"📦 Stock: {producto_actual['cantidad']} unidades\n"
            f"{f'🏷️ Etiqueta: {etiqueta}' if etiqueta else ''}\n\n"
        )

        datos_temp["inv_producto_actual"] = {}
        datos_temp["inv_sub_paso"] = SUB_PASO_CONTINUAR
        await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)

        if productos_count == 1:
            mensaje_progreso = "📦 Has cargado tu inventario inicial correctamente 🎉\n\n"
        else:
            mensaje_progreso = f"Llevas *{productos_count}* producto(s) cargado(s) 🎉\n\n"

        return (
            resumen +
            mensaje_progreso +
            "¿Qué hacemos?\n"
            "➕ Escribe *otro* para agregar más\n"
            "✅ Escribe *listo* para continuar"
        )

    def _normalizar_tipo_ropa(self, texto: str) -> str:
        return texto.strip().lower()

    def _detectar_confirmacion(self, mensaje: str) -> bool:
        return mensaje.strip().lower() in PALABRAS_CONFIRMAR

    def _formatear_lista_categorias(self, categorias: list[str]) -> str:
        return "\n".join(f"{i+1}. {c}" for i, c in enumerate(categorias))

    def _filtrar_categorias_validas(self, categorias: list[str]) -> list[str]:
        permitidas = {
            # ───────── SUPERIORES ─────────
            "polo", "polos", "camiseta", "camisetas", "camisa", "camisas",
            "blusa", "blusas", "top", "tops", "crop top", "croptop",
            "casaca", "casacas", "chaqueta", "chaquetas", "abrigo", "abrigos",
            "chompa", "chompas", "suéter", "sueter", "suéteres", "sueteres",
            "polera", "poleras", "hoodie", "hoodies",
            "cardigan", "cárdigan", "cardigans", "cárdigans",
            "chaleco", "chalecos",
            "blazer", "blazers",
            "camisilla", "camisillas", "bvd", "bividi",
            "jersey", "jerseys",
            "poncho", "ponchos",
            "casaca jean", "casacas jean", "chaqueta jean",
            # ───────── INFERIORES ─────────
            "pantalon", "pantalones", "pantalón", "pantalones",
            "jean", "jeans", "vaquero", "vaqueros",
            "short", "shorts", "bermuda", "bermudas",
            "falda", "faldas", "minifalda", "minifaldas",
            "maxifalda", "maxifaldas",
            "leggins", "legging", "leggings",
            "buzo", "buzos", "jogger", "joggers",
            "pantalon deportivo", "pantalones deportivos",
            "pantalon jogger", "pantalones jogger",
            "licra", "licras", "malla", "mallas",
            "palazzo", "palazzos",
            "culotte", "culottes",
            "capri", "capris",
            "overol", "overoles", "enterizo", "enterizos",
        }

        categorias_validas = []
        for c in categorias:
            c_norm = c.strip().lower()
            if c_norm in permitidas:
                categorias_validas.append(c.strip())
                continue
            for p in permitidas:
                if p in c_norm:
                    categorias_validas.append(c.strip())
                    break

        categorias_validas = list(dict.fromkeys(categorias_validas))

        if not categorias_validas:
            return ["Polos", "Pantalones", "Shorts", "Blusas"]

        return categorias_validas

    async def _obtener_o_generar_categorias(self, tipo_ropa: str) -> list[str]:
        tipo_normalizado = self._normalizar_tipo_ropa(tipo_ropa)

        categorias_cached = await self.buscar_plantilla_categorias(tipo_normalizado)
        if categorias_cached:
            logger.info(f"[Categorías] Caché hit para tipo_ropa='{tipo_normalizado}'")
            categorias_filtradas = self._filtrar_categorias_validas(categorias_cached)
            await self.guardar_plantilla_categorias(tipo_normalizado, categorias_filtradas)
            return categorias_filtradas

        logger.info(f"[Categorías] Caché miss para tipo_ropa='{tipo_normalizado}'. Generando con IA...")

        result = await gemini_service.generar_categorias_por_tipo_ropa(
            tipo_ropa=f"{tipo_ropa}. SOLO incluir prendas superiores e inferiores. NO incluir calzado ni accesorios."
        )

        categorias_nuevas = result.get("categorias", [
            "Polos", "Pantalones", "Blusas", "Shorts"
        ])

        categorias_filtradas = self._filtrar_categorias_validas(categorias_nuevas)
        await self.guardar_plantilla_categorias(tipo_normalizado, categorias_filtradas)
        return categorias_filtradas

    # ──────────────────────────────────────────────
    #  HELPERS — parseo de datos de inventario
    # ──────────────────────────────────────────────

    def _parsear_precio(self, texto: str) -> float | None:
        """Intenta extraer un número decimal del texto ingresado."""
        import re
        texto_limpio = texto.replace(",", ".").strip()
        match = re.search(r"\d+(\.\d+)?", texto_limpio)
        if match:
            return float(match.group())
        return None

    def _parsear_cantidad(self, texto: str) -> int | None:
        """Intenta extraer un entero del texto ingresado."""
        import re
        match = re.search(r"\d+", texto.strip())
        if match:
            return int(match.group())
        return None

    def _es_omision(self, texto: str) -> bool:
        """Retorna True si el usuario quiere omitir un campo opcional."""
        omisiones = {"no", "ninguno", "ninguna", "omitir", "saltar", "-", "n/a", "nada", "sin etiqueta"}
        return texto.strip().lower() in omisiones

    # ──────────────────────────────────────────────
    #  FLUJO PRINCIPAL
    # ──────────────────────────────────────────────

    async def procesar(self, telefono: str, mensaje: str) -> str:
        """
        Punto de entrada. Determina el paso actual y responde.
        Retorna el texto a enviar por WhatsApp.
        """
        negocio = await self.get_negocio(telefono)

        # ── NEGOCIO NUEVO: crear registro y dar bienvenida ──
        if not negocio:
            negocio_id = await self.crear_negocio(telefono)
            await self.upsert_sesion(negocio_id, "onboarding_1", {})
            return (
                "¡Hola! Qué gusto saludarte. 👋 Soy *Quri* y estoy aquí para que hagamos "
                "crecer tu negocio juntos. 🚀\n\n"
                "Puedo ayudarte a registrar tus *ventas y gastos*, y llevar un control "
                "de tu *inventario* de forma fácil. ✨\n\n"
                "Para comenzar, ¿cómo se llama tu tienda de ropa?"
            )

        negocio_id = str(negocio["id"])
        sesion = await self.get_sesion(telefono)
        estado = sesion.get("estado_conversacion", "onboarding_1") if sesion else "onboarding_1"
        datos_temp = self._leer_datos_temp(sesion)

        logger.info(f"[Onboarding] {telefono} | estado={estado} | msg='{mensaje}'")

        # ══════════════════════════════════════════
        #  PASO 1 → Capturar nombre del negocio
        # ══════════════════════════════════════════
        if estado == "onboarding_1":
            nombre_negocio = await gemini_service.extraer_dato(
                campo="nombre del negocio",
                mensaje=mensaje,
            )
            datos_temp["nombre_negocio"] = nombre_negocio
            await self.actualizar_negocio(negocio_id, nombre_negocio=nombre_negocio)
            await self.upsert_sesion(negocio_id, "onboarding_2", datos_temp)
            return (
                f"Perfecto, *{nombre_negocio}* quedó registrado. 🎉\n\n"
                "¿Y cuál es tu *nombre*? (el del dueño o encargado)"
            )

        # ══════════════════════════════════════════
        #  PASO 2 → Capturar nombre del propietario
        # ══════════════════════════════════════════
        elif estado == "onboarding_2":
            nombre_propietario = await gemini_service.extraer_dato(
                campo="nombre de la persona",
                mensaje=mensaje,
            )
            datos_temp["nombre_propietario"] = nombre_propietario
            await self.actualizar_negocio(negocio_id, nombre_propietario=nombre_propietario)
            await self.upsert_sesion(negocio_id, "onboarding_3", datos_temp)
            return (
                f"Mucho gusto, *{nombre_propietario}* 😊\n\n"
                "¿Qué *monedas* acepta tu negocio?\n\n"
                "1️⃣ Solo *Soles* (PEN)\n"
                "2️⃣ Solo *Pesos chilenos* (CLP)\n"
                "3️⃣ *Ambas* (Soles y Pesos)"
            )

        # ══════════════════════════════════════════
        #  PASO 3 → Capturar monedas aceptadas
        # ══════════════════════════════════════════
        elif estado == "onboarding_3":
            monedas = await gemini_service.extraer_monedas(mensaje=mensaje)
            datos_temp["monedas_aceptadas"] = monedas
            await self.actualizar_negocio(negocio_id, monedas_aceptadas=monedas)

            atiende_chilenos = "CLP" in monedas
            await self.actualizar_negocio(
                negocio_id,
                atiende_turistas_chilenos=atiende_chilenos,
            )

            await self.upsert_sesion(negocio_id, "onboarding_3b", datos_temp)

            monedas_texto = {
                "PEN": "solo Soles 🇵🇪",
                "CLP": "solo Pesos chilenos 🇨🇱",
                "PEN,CLP": "Soles y Pesos chilenos 🇵🇪🇨🇱",
            }.get(monedas, monedas)

            return (
                f"Anotado, aceptas *{monedas_texto}* 💰\n\n"
                "¿A qué hora sueles *cerrar tu tienda*? 🕐\n"
                "_(Ej: 8pm, 20:00, 9 de la noche)_"
            )

        # ══════════════════════════════════════════
        #  PASO 3b → Capturar horario de cierre
        # ══════════════════════════════════════════
        elif estado == "onboarding_3b":
            msg_lower = mensaje.strip().lower()
            
            # ── NUEVO: detectar si quiere corregir la moneda ──
            quiere_corregir_moneda = any(w in msg_lower for w in [
                "no,", "solo soles", "soles noma", "soles nomás", "me equivoqué",
                "equivoqué", "error", "solo pen", "corrección", "corregir moneda"
            ])
            if quiere_corregir_moneda or (
                any(w in msg_lower for w in ["solo soles", "soles", "pen"]) and
                "clp" not in msg_lower and datos_temp.get("monedas_aceptadas") == "PEN,CLP"
            ):
                monedas = await gemini_service.extraer_monedas(mensaje=mensaje)
                datos_temp["monedas_aceptadas"] = monedas
                await self.actualizar_negocio(negocio_id, monedas_aceptadas=monedas)
                monedas_texto = {
                    "PEN": "solo Soles 🇵🇪",
                    "CLP": "solo Pesos chilenos 🇨🇱",
                    "PEN,CLP": "Soles y Pesos chilenos 🇵🇪🇨🇱",
                }.get(monedas, monedas)
                return (
                    f"Corregido, aceptas *{monedas_texto}* 💰\n\n"
                    "¿A qué hora sueles *cerrar tu tienda*? 🕐\n"
                    "_(Ej: 8pm, 20:00, 9 de la noche)_"
                )
            horario = await self._extraer_y_validar_horario(mensaje)
            
            if not horario:
                return (
                    "No entendí el horario 😅 ¿A qué hora cierras?\n"
                    "_(Ej: *8pm*, *20:00*, *9 de la noche*)_"
                )
            
            datos_temp["horario_cierre"] = horario
            await self.actualizar_negocio(negocio_id, horario_cierre=horario)
            await self.upsert_sesion(negocio_id, "onboarding_4", datos_temp)
            return (
                f"Perfecto, cierre a las *{horario}* 🕐 Anotado.\n\n"
                "¿Y qué *tipo de ropa* vende tu negocio? 👗\n\n"
                "1. Ropa de Dama\n"
                "2. Ropa de Varón\n"
                "3. Ropa de Niños\n"
                "4. Ropa Deportiva\n"
                "5. Ropa en General (Dama, Varón, Niños)\n"   # ← AGREGAR
                "6. ¡Otro! Cuéntame qué tipo de ropa es ✨"    # ← era 5, ahora 6
            )

        # ══════════════════════════════════════════
        #  PASO 4 → Capturar tipo de ropa
        # ══════════════════════════════════════════
        elif estado == "onboarding_4":
            msg_strip = mensaje.strip()
            mapa_opciones = {
                "1": "Ropa de Dama",
                "2": "Ropa de Varón",
                "3": "Ropa de Niños",
                "4": "Ropa Deportiva",
                "5": "Ropa en General",
            }
            if msg_strip in mapa_opciones:
                tipo_ropa = mapa_opciones[msg_strip]
            else:
                tipo_ropa = await gemini_service.extraer_dato(
                    campo="tipo de ropa o categoría de ropa",
                    mensaje=mensaje,
                )
                tipo_ropa = tipo_ropa[:50]

            datos_temp["rubro"] = tipo_ropa
            await self.actualizar_negocio(negocio_id, rubro=tipo_ropa)

            categorias = await self._obtener_o_generar_categorias(tipo_ropa)
            datos_temp["categorias_propuestas"] = categorias

            await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)

            lista = self._formatear_lista_categorias(categorias)
            return (
                f"Para organizar mejor tu negocio, "
                f"te recomiendo empezar con estas categorías 👗\n\n"
                f"{lista}\n\n"
                "¿Las dejamos así o quieres quitar/agregar algo?"
            )
        # ══════════════════════════════════════════
        #  PASO 5 → Ajustar categorías hasta "listo"
        # ══════════════════════════════════════════
        elif estado == "onboarding_5":
            categorias = datos_temp.get("categorias_propuestas", [])
            ya_hizo_cambios = datos_temp.get("categorias_editadas", False)

            # ── Interpretar intención con mini-prompt de IA ──
            interpretacion = await gemini_service.interpretar_accion_categoria(
                mensaje=mensaje,
                categorias_actuales=categorias,
            )
            accion = interpretacion.get("accion", "DESCONOCIDO")
            valor  = interpretacion.get("valor")

            logger.info(
                f"[Onboarding5] accion={accion} valor={valor!r} "
                f"confianza={interpretacion.get('confianza')}"
            )

            # ── CONFIRMAR: guardar y preguntar por inventario ──
            if accion == "CONFIRMAR":
                await self.guardar_categorias_negocio(negocio_id, categorias)
                await self.upsert_sesion(negocio_id, "onboarding_5b", datos_temp)
                return (
                    "¡Perfecto! 📦 Categorías guardadas.\n\n"
                    "¿Quieres cargar tu inventario ahora?\n\n"
                    "1️⃣ *Sí, ahora* — te guío producto a producto\n"
                    "2️⃣ *Después* — lo iré registrando mientras vendo\n"
                    "3️⃣ *Dashboard* — lo cargo con calma desde la web"
                )

            # ── AGREGAR: añadir categoría genérica ──
            if accion == "AGREGAR" and valor:
                nueva = str(valor).strip().title()
                if nueva not in categorias:
                    categorias.append(nueva)
                    datos_temp["categorias_propuestas"] = categorias
                    datos_temp["categorias_editadas"] = True
                    await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                    lista = self._formatear_lista_categorias(categorias)
                    return (
                        f"✅ *{nueva}* agregada 👗\n\n"
                        f"{lista}\n\n"
                        "¿Algún otro cambio o seguimos?"
                    )
                else:
                    lista = self._formatear_lista_categorias(categorias)
                    return (
                        f"*{nueva}* ya está en la lista 😊\n\n"
                        f"{lista}\n\n"
                        "¿Algún otro cambio o seguimos?"
                    )

            # ── QUITAR: eliminar por número o nombre ──
            if accion == "QUITAR" and valor is not None:
                if isinstance(valor, int):
                    idx = valor - 1
                    if 0 <= idx < len(categorias):
                        eliminada = categorias.pop(idx)
                        datos_temp["categorias_propuestas"] = categorias
                        datos_temp["categorias_editadas"] = True
                        await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                        lista = self._formatear_lista_categorias(categorias)
                        return (
                            f"🗑️ *{eliminada}* quitada.\n\n"
                            f"{lista}\n\n"
                            "¿Algún otro cambio o seguimos?"
                        )
                    else:
                        return (
                            f"⚠️ No existe el número {valor}. "
                            f"Elige entre 1 y {len(categorias)}."
                        )
                else:
                    # Intentar quitar por nombre
                    nombre_buscar = str(valor).strip().lower()
                    idx_encontrado = next(
                        (i for i, c in enumerate(categorias) if c.lower() == nombre_buscar),
                        None,
                    )
                    if idx_encontrado is not None:
                        eliminada = categorias.pop(idx_encontrado)
                        datos_temp["categorias_propuestas"] = categorias
                        datos_temp["categorias_editadas"] = True
                        await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                        lista = self._formatear_lista_categorias(categorias)
                        return (
                            f"🗑️ *{eliminada}* quitada.\n\n"
                            f"{lista}\n\n"
                            "¿Algún otro cambio o seguimos?"
                        )
                    else:
                        lista = self._formatear_lista_categorias(categorias)
                        return (
                            f"⚠️ No encontré esa categoría en la lista.\n\n"
                            f"{lista}\n\n"
                            "Dime el *número* de la que quieres quitar 😊"
                        )

            # ── REEMPLAZAR: cambiar nombre ──
            if accion == "REEMPLAZAR" and isinstance(valor, dict):
                viejo = str(valor.get("viejo", "")).strip().lower()
                nuevo = str(valor.get("nuevo", "")).strip().title()
                
                # Buscar por índice (si viejo es un número) o por nombre
                idx_encontrado = None
                try:
                    idx = int(viejo) - 1
                    if 0 <= idx < len(categorias):
                        idx_encontrado = idx
                except ValueError:
                    idx_encontrado = next(
                        (i for i, c in enumerate(categorias) if c.lower() == viejo),
                        None,
                    )
                    
                if idx_encontrado is not None and nuevo:
                    anterior = categorias[idx_encontrado]
                    categorias[idx_encontrado] = nuevo
                    datos_temp["categorias_propuestas"] = categorias
                    datos_temp["categorias_editadas"] = True
                    await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                    lista = self._formatear_lista_categorias(categorias)
                    return (
                        f"✏️ Listo, cambié *{anterior}* por *{nuevo}*.\n\n"
                        f"{lista}\n\n"
                        "¿Algún otro cambio o seguimos?"
                    )
                else:
                    lista = self._formatear_lista_categorias(categorias)
                    return (
                        f"⚠️ No encontré la categoría para cambiar.\n\n"
                        f"{lista}\n\n"
                        "Dime el *número* o nombre exacto de la que quieres cambiar 😊"
                    )

            # ── PRODUCTO: el usuario confundió categoría con producto ──
            if accion == "PRODUCTO":
                lista = self._formatear_lista_categorias(categorias)
                return (
                    "¡Qué buena onda! 😊 Más adelante vas a poder agregar ese producto "
                    "con todos sus detalles (talla, precio, stock...).\n\n"
                    "Por ahora solo definimos las *categorías* para clasificar tu ropa, "
                    "que son nombres generales como Blusas, Jeans o Shorts.\n\n"
                    f"Tu lista por ahora es:\n{lista}\n\n"
                    "¿Seguimos editando o quedan estas categorías?"
                )

            # ── DESCONOCIDO: ayuda contextual ──
            lista = self._formatear_lista_categorias(categorias)
            if ya_hizo_cambios:
                return (
                    f"Estas son tus categorías actuales 👗\n\n"
                    f"{lista}\n\n"
                    "¿Algún otro cambio o seguimos?"
                )
            return (
                f"No entendí bien 😅 Aquí está la lista actual:\n\n"
                f"{lista}\n\n"
                "Puedes decirme, por ejemplo:\n"
                "➕ \"agregar Shorts\"\n"
                "➖ \"quitar 3\"\n"
                "✅ \"listo\" cuando estén perfectas"
            )

        # ══════════════════════════════════════════
        #  PASO 5b → Decidir cuándo cargar inventario
        # ══════════════════════════════════════════
        elif estado == "onboarding_5b":
            msg_lower = mensaje.strip().lower()

            # Detectar opción 1: cargar ahora
            cargar_ahora = any(p in msg_lower for p in [
                "1", "sí", "si", "ahora", "ahora mismo", "quiero", "vamos", "dale",
            ])
            # Detectar opción 2 o 3: cargar después o desde dashboard
            cargar_despues = any(p in msg_lower for p in [
                "2", "3", "después", "despues", "luego", "mas tarde", "más tarde",
                "dashboard", "web", "no", "nope",
            ])

            if cargar_ahora and not cargar_despues:
                datos_temp["inv_productos_cargados"] = datos_temp.get("inv_productos_cargados", 0)
                datos_temp["formulario_producto"]    = {}
                datos_temp["inv_confirmado"]         = False
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return (
                    "¡Genial! Vamos a cargar tu inventario 📦\n\n"
                    "Puedes escribirlo todo junto o poco a poco 👇\n\n"
                    "📝 *Nombre* — ej: Polo básico, Jean slim\n"
                    "📐 *Talla* — ej: S, M, L, XL, 28, 30 (o 'talla única')\n"
                    "💰 *Precio de venta* — ej: S/ 35\n"
                    "📦 *Stock* — cuántas unidades tienes\n"
                    "💵 *Precio de compra* — opcional\n\n"
                    "_(Ej: Jean slim talla 28, precio 45 soles, stock 20)_"
                )

            elif cargar_despues:
                # Detectar si eligió dashboard (opción 3) o después (opción 2)
                es_dashboard = any(p in msg_lower for p in ["3", "dashboard", "web"])
                await self.upsert_sesion(negocio_id, "onboarding_6", datos_temp)

                if es_dashboard:
                    return (
                        "¡Perfecto! 🖥️ Puedes cargar tu inventario completo desde aquí:\n\n"
                        "👉 http://bit.ly/4dIQAVB\n\n"
                        "*Última pregunta: ¿A qué hora cierras tu tienda*?\n"
                        "_(Ej: 8pm, 20:00, 9 de la noche)_"
                    )
                return (
                    "Sin problema, puedes cargarlo cuando quieras 😊\n\n"
                    "*Última pregunta: ¿A qué hora cierras tu tienda*?\n"
                    "_(Ej: 8pm, 20:00, 9 de la noche)_"
                )

            else:
                # No se entendió la opción
                return (
                    "No entendí bien 😅 Elige una opción:\n\n"
                    "1️⃣ *Sí, ahora* — te guío producto a producto\n"
                    "2️⃣ *Después* — lo registro mientras vendo\n"
                    "3️⃣ *Dashboard* — lo cargo desde la web"
                )

        # ══════════════════════════════════════════
        #  PASO 5c → Bucle de carga de productos
        # ══════════════════════════════════════════

        elif estado == "onboarding_5c":
            from app.graph.nodes.producto_guiado import agregar_producto_guiado

            msg_lower       = mensaje.strip().lower()
            productos_count = datos_temp.get("inv_productos_cargados", 0)
            formulario_activo = bool(datos_temp.get("formulario_producto"))

            nombre_prop    = datos_temp.get("nombre_propietario", "")
            nombre_negocio = datos_temp.get("nombre_negocio", "tu negocio")
            prod_txt = f"Ya cargaste *{productos_count}* producto(s). " if productos_count > 0 else ""

            # ── ESCAPE 1: quiere usar el Dashboard (siempre, incluso mid-form) ──
            quiere_dashboard = any(w in msg_lower for w in [
                "dashboard", "web", "página", "pagina", "sistema", "computadora",
                "mejor lo hago", "lo hago desde", "lo cargo desde",
            ])
            if quiere_dashboard:
                await self.completar_onboarding(negocio_id)
                await self.upsert_sesion(negocio_id, "activo", {})
                return (
                    f"¡Sin problema! 🖥️ {prod_txt}Puedes cargar el resto de tu inventario "
                    f"con calma desde el dashboard:\n\n"
                    f"👉 http://bit.ly/4dIQAVB\n\n"
                    f"*{nombre_negocio}* ya está configurado en Quri, {nombre_prop}. 🎉\n\n"
                    "Recuerda que cuando quieras registrar una venta, un gasto o "
                    "consultar tu stock, solo escríbeme aquí. 💪"
                )

            # ── ESCAPE 2: quiere hacerlo después (siempre, incluso mid-form) ──
            quiere_despues = any(w in msg_lower for w in [
                "después", "despues", "luego", "más tarde", "mas tarde",
                "ahorita no", "otro momento", "ahora no", "lo hago después",
                "lo dejo para", "mañana", "después lo",
            ])
            if quiere_despues:
                await self.completar_onboarding(negocio_id)
                await self.upsert_sesion(negocio_id, "activo", {})
                return (
                    f"¡Perfecto, sin apuro! 😊 {prod_txt}\n\n"
                    f"*{nombre_negocio}* ya está configurado en Quri, {nombre_prop}. 🎉\n\n"
                    "Puedes ir registrando tus productos cuando quieras. "
                    "Por ahora, cuando necesites registrar una venta, un gasto "
                    "o consultar algo, solo escríbeme aquí. 💪"
                )

            # ── ESCAPE 3: "listo" / "no" / "terminar" — solo si no hay formulario activo ──
            if not formulario_activo and any(w in msg_lower for w in [
                "no", "listo", "terminar", "finalizar", "ya no", "hasta aqui", "hasta aquí"
            ]):
                await self.completar_onboarding(negocio_id)
                await self.upsert_sesion(negocio_id, "activo", {})
                return (
                    f"¡Todo listo, {nombre_prop}! 🎉\n\n"
                    f"*{nombre_negocio}* ya está configurado en Quri. {prod_txt}\n\n"
                    "Ahora puedes registrar tus operaciones:\n"
                    "📦 _'Vendí 3 polos a S/25 cada uno'_\n"
                    "💸 _'Gasté S/200 en mercadería'_\n"
                    "📊 _'¿Cuánto vendí hoy?'_\n\n"
                    "¡Estoy aquí para ayudarte! 💪"
                )

            # ── ESCAPE 4: "agregar otro" / "si" / "claro" — solo si no hay formulario activo ──
            if not formulario_activo and any(w in msg_lower for w in [
                "agregar otro", "otro", "sí", "si", "claro", "dale", "siguiente"
            ]) and len(msg_lower) <= 15:
                return (
                    f"¡Genial! 📦 Llevas *{productos_count}* producto(s) cargado(s).\n\n"
                    "Cuéntame los datos del siguiente producto:\n"
                    "📝 *Nombre* (ej: Polo básico)\n"
                    "📐 *Talla* (ej: M, 28, o Única)\n"
                    "💰 *Precio de venta*\n"
                    "📦 *Stock*\n"
                    "💵 *Precio de compra* (opcional)\n\n"
                    "_(Ej: Polo básico talla M, precio 35, stock 50)_"
                )

            resultado = await agregar_producto_guiado(negocio_id, mensaje, datos_temp)

            if resultado["finalizado"]:
                productos_count += 1
                datos_temp["inv_productos_cargados"] = productos_count
                # Limpiar formulario para el próximo producto
                datos_temp["formulario_producto"] = {}
                datos_temp["inv_confirmado"]       = False

                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)

                if resultado.get("agregar_otro"):
                    return (
                        resultado["respuesta"] +
                        f"\n\n📦 Llevas *{productos_count}* producto(s) 🎉\n"
                        "Cuéntame los datos del siguiente producto:"
                    )

                return (
                    resultado["respuesta"] +
                    f"\n\n📦 Llevas *{productos_count}* producto(s) cargado(s) 🎉\n"
                    "¿Quieres *agregar otro*, hacerlo desde el *dashboard* o escribes *listo* para terminar?"
                )

            # Flujo intermedio — persistir datos parciales y seguir
            datos_temp.update(resultado["datos"])
            await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
            return resultado["respuesta"]


        # ══════════════════════════════════════════
        #  PASO 6 → Capturar horario de cierre
        # ══════════════════════════════════════════
        elif estado == "onboarding_6":
            # Horario ya guardado en onboarding_3b — aquí solo cerramos el onboarding
            await self.completar_onboarding(negocio_id)
            await self.upsert_sesion(negocio_id, "activo", {})

            nombre_negocio = datos_temp.get("nombre_negocio", "tu negocio")
            nombre_prop    = datos_temp.get("nombre_propietario", "")
            return (
                f"¡Todo listo, {nombre_prop}! 🎉\n\n"
                f"*{nombre_negocio}* ya está configurado en Quri.\n\n"
                "Ahora puedes registrar tus operaciones fácilmente:\n"
                "📦 _'Vendí 3 polos a S/25 cada uno'_\n"
                "💸 _'Gasté S/200 en mercadería'_\n"
                "📊 _'¿Cuánto vendí hoy?'_\n\n"
                "¡Estoy aquí para ayudarte! 💪"
            )


        # ── Estado desconocido: reiniciar ──
        else:
            await self.upsert_sesion(negocio_id, "onboarding_1", {})
            return "¡Hola! Soy Quri. ¿Cómo se llama tu negocio? 😊"


onboarding_service = OnboardingService()