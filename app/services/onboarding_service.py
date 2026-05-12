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
                json.dumps(datos_temp or {}, default=_json_serial),
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
        etiqueta: str | None = None,
        categoria_nombre: str | None = None,
    ) -> str:
        """
        Inserta un producto en `productos` y crea su registro en `stock`.
        Retorna el producto_id creado.
        El campo nombre_variantes se usa para almacenar la talla.
        La etiqueta se guarda como variante adicional si fue provista.
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
                    (negocio_id, nombre, nombre_variantes, precio_venta_pen, unidad, activo, categoria_id)
                VALUES
                    ($1, $2, $3, $4, 'unidad', true, $5)
                RETURNING id
                """,
                negocio_id,
                nombre.strip(),
                variantes,
                precio_venta,
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

    def _json_serial(obj):
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
            "Escríbeme los datos así, en líneas separadas:\n\n"
            "📝 *Nombre:* Polo básico\n"
            "📐 *Talla:* M\n"
            "💰 *Precio:* 35\n"
            "📦 *Stock:* 10\n"
            "🏷️ *Etiqueta:* verano24 _(opcional, escribe «no» si no quieres)_\n\n"
            "Puedes copiar ese formato y reemplazar los valores. "
            "Si algo te falta saber, también puedes escribir solo lo que tengas y te ayudo con lo demás 😊"
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
        datos = self._leer_datos_temp(sesion)
        historial = datos.get("historial_mensajes", [])

        historial.append({"role": "user",      "content": user_msg})
        historial.append({"role": "assistant",  "content": bot_msg})

        max_mensajes = max_turns * 2
        if len(historial) > max_mensajes:
            historial = historial[-max_mensajes:]

        datos["historial_mensajes"] = historial

        estado_actual = (
            sesion.get("estado_conversacion", "activo") if sesion else "activo"
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
                "¡Hola! 👋 Soy *Quri*, tu asistente de negocios para gestionar "
                "ventas, gastos e inventario por WhatsApp.\n\n"
                "¿Cuál es el *nombre de tu negocio* de ropa en Tacna?"
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

            await self.upsert_sesion(negocio_id, "onboarding_4", datos_temp)

            monedas_texto = {
                "PEN": "solo Soles 🇵🇪",
                "CLP": "solo Pesos chilenos 🇨🇱",
                "PEN,CLP": "Soles y Pesos chilenos 🇵🇪🇨🇱",
            }.get(monedas, monedas)

            return (
                f"Anotado, aceptas *{monedas_texto}* 💰\n\n"
                "¿Qué *tipo de ropa* vende tu negocio?\n"
                "_(Ej: ropa para niños, dama, caballero, ropa deportiva, etc.)_"
            )

        # ══════════════════════════════════════════
        #  PASO 4 → Capturar tipo de ropa
        # ══════════════════════════════════════════
        elif estado == "onboarding_4":
            tipo_ropa = await gemini_service.extraer_dato(
                campo="tipo de ropa o categoría de ropa",
                mensaje=mensaje,
            )
            datos_temp["rubro"] = tipo_ropa
            await self.actualizar_negocio(negocio_id, rubro=tipo_ropa)

            categorias = await self._obtener_o_generar_categorias(tipo_ropa)
            datos_temp["categorias_propuestas"] = categorias

            await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)

            lista = self._formatear_lista_categorias(categorias)
            return (
                f"Genial 👗 Para *{tipo_ropa}* te sugiero estas categorías de inventario:\n\n"
                f"{lista}\n\n"
                "Puedes:\n"
                "➕ *Agregar* escribiendo: _agregar Shorts_\n"
                "➖ *Quitar* escribiendo: _quitar 3_ (el número)\n"
                "✅ Escribe *listo* cuando estén perfectas"
            )

        # ══════════════════════════════════════════
        #  PASO 5 → Ajustar categorías hasta "listo"
        # ══════════════════════════════════════════
        elif estado == "onboarding_5":
            categorias = datos_temp.get("categorias_propuestas", [])
            msg_lower = mensaje.strip().lower()

            # ── Confirmación ──
            if self._detectar_confirmacion(msg_lower):
                await self.guardar_categorias_negocio(negocio_id, categorias)
                await self.upsert_sesion(negocio_id, "onboarding_5b", datos_temp)
                return (
                    "¡Perfecto! 📦 Categorías guardadas.\n\n"
                    "¿Quieres cargar tu inventario ahora?\n\n"
                    "1️⃣ *Sí, ahora* — te guío producto a producto\n"
                    "2️⃣ *Después* — lo iré registrando mientras vendo\n"
                    "3️⃣ *Dashboard* — lo cargo con calma desde la web"
                )

            # ── Agregar categoría ──
            if msg_lower.startswith("agregar "):
                nueva = mensaje[8:].strip().title()
                if nueva and nueva not in categorias:
                    categorias.append(nueva)
                datos_temp["categorias_propuestas"] = categorias
                await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                lista = self._formatear_lista_categorias(categorias)
                return (
                    f"✅ *{nueva}* agregada.\n\n"
                    f"{lista}\n\n"
                    "Escribe *listo* para confirmar o sigue ajustando."
                )

            # ── Quitar por número ──
            if msg_lower.startswith("quitar "):
                numero_str = mensaje[7:].strip()
                if numero_str.isdigit():
                    idx = int(numero_str) - 1
                    if 0 <= idx < len(categorias):
                        eliminada = categorias.pop(idx)
                        datos_temp["categorias_propuestas"] = categorias
                        await self.upsert_sesion(negocio_id, "onboarding_5", datos_temp)
                        lista = self._formatear_lista_categorias(categorias)
                        return (
                            f"🗑️ *{eliminada}* eliminada.\n\n"
                            f"{lista}\n\n"
                            "Escribe *listo* para confirmar o sigue ajustando."
                        )
                    else:
                        return f"⚠️ No existe el número {numero_str}. Elige entre 1 y {len(categorias)}."
                else:
                    return "⚠️ Para quitar escribe el *número* de la categoría. Ej: _quitar 2_"

            # ── Mensaje no reconocido ──
            lista = self._formatear_lista_categorias(categorias)
            return (
                f"No entendí bien 😅 Estas son tus categorías actuales:\n\n"
                f"{lista}\n\n"
                "Puedes escribir:\n"
                "➕ _agregar NombreCategoria_\n"
                "➖ _quitar N_ (el número)\n"
                "✅ *listo* para confirmar"
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
                # Iniciar sub-flujo de carga de productos
                datos_temp["inv_sub_paso"]         = SUB_PASO_COMPLETO
                datos_temp["inv_producto_actual"]  = {}
                datos_temp["inv_productos_cargados"] = datos_temp.get("inv_productos_cargados", 0)
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return self._mensaje_plantilla_producto(1)

            elif cargar_despues:
                # Saltar directamente al paso 6
                await self.upsert_sesion(negocio_id, "onboarding_6", datos_temp)
                return (
                    "Sin problema, puedes cargarlo cuando quieras 😊\n\n"
                    "Última pregunta: ¿A qué hora *cierras tu tienda*?\n"
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
            sub_paso         = datos_temp.get("inv_sub_paso", SUB_PASO_NOMBRE)
            producto_actual  = datos_temp.get("inv_producto_actual", {})
            productos_count  = datos_temp.get("inv_productos_cargados", 0)
            msg_lower        = mensaje.strip().lower()

            # ── Sub-paso: parsear respuesta del formulario completo ──
            if sub_paso == SUB_PASO_COMPLETO:

                texto = mensaje.strip()

                # heurística rápida: si no viene con etiquetas pero parece frase
                if ":" not in texto:
                    partes = texto.lower()

                    # nombre (todo antes de "talla" o "precio")
                    if "talla" in partes:
                        nombre = partes.split("talla")[0].strip()
                        producto_actual["nombre"] = nombre.title()

                    if "talla" in partes:
                        import re
                        m = re.search(r"talla\s*([a-z0-9]+)", partes)
                        if m:
                            producto_actual["talla"] = m.group(1).upper()

                    if "precio" in partes:
                        producto_actual["precio_venta"] = self._parsear_precio(partes)

                    match = re.search(r"(stock|tengo)\D*(\d+)", partes)
                    if match:
                        producto_actual["cantidad"] = int(match.group(2))
                    
                campos = self._parsear_formulario_producto(mensaje)

                # Guardar lo que llegó en producto_actual
                if "nombre" in campos and campos["nombre"]:
                    producto_actual["nombre"] = campos["nombre"].title()
                if "talla" in campos and campos["talla"]:
                    producto_actual["talla"] = campos["talla"].upper()
                if "precio" in campos and campos["precio"]:
                    producto_actual["precio_venta"] = campos["precio"]
                # 1. intentar detectar contexto primero
                match = re.search(r"(stock|tengo|hay|quedan|me quedan)\D*(\d+)", partes)

                if match:
                    producto_actual["cantidad"] = int(match.group(2))
                else:
                    # 2. fallback: tomar el último número del mensaje
                    numeros = re.findall(r"\d+", partes)

                    if numeros:
                        if len(numeros) > 1:
                            producto_actual["cantidad"] = int(numeros[-1])
                        else:
                            producto_actual["cantidad"] = int(numeros[0])
                # etiqueta puede ser None explícito (omisión)
                if "etiqueta" in campos:
                    producto_actual["etiqueta"] = campos["etiqueta"]

                datos_temp["inv_producto_actual"] = producto_actual

                if (
                    producto_actual.get("nombre")
                    and producto_actual.get("talla")
                    and producto_actual.get("precio_venta")
                    and producto_actual.get("cantidad") is not None
                ):
                    etiqueta = producto_actual.get("etiqueta")
                    return await self._guardar_y_confirmar_producto(
                        negocio_id, datos_temp, producto_actual, etiqueta, productos_count
                    )

                # ── Determinar qué falta y pedir solo eso ──
                if not producto_actual.get("nombre"):
                    datos_temp["inv_sub_paso"] = SUB_PASO_TALLA  # usamos talla como proxy para re-pedir nombre
                    datos_temp["inv_sub_paso"] = "nombre_faltante"
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        "Casi listo 😊 Solo me falta saber:\n\n"
                        "📝 ¿Cómo se llama el producto?\n"
                        "_(Ej: Polo básico, Jean slim, Blusa floral)_"
                    )

                if not producto_actual.get("talla"):
                    datos_temp["inv_sub_paso"] = SUB_PASO_TALLA
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"Bien, *{producto_actual['nombre']}* anotado ✅\n\n"
                        "📐 ¿Qué *talla* tiene?\n"
                        "_(Ej: S, M, L, XL, 28, 30, Talla única)_"
                    )

                if not producto_actual.get("precio_venta"):
                    datos_temp["inv_sub_paso"] = SUB_PASO_PRECIO
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"*{producto_actual['nombre']}* talla *{producto_actual['talla']}* ✅\n\n"
                        "💰 ¿Cuál es el *precio de venta* en Soles?\n"
                        "_(Ej: 35, 49.90, 120)_"
                    )

                if producto_actual.get("cantidad") is None:
                    datos_temp["inv_sub_paso"] = SUB_PASO_STOCK
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"*{producto_actual['nombre']}* · S/ {producto_actual['precio_venta']:.2f} ✅\n\n"
                        "📦 ¿Cuántas *unidades* tienes en stock ahora?\n"
                        "_(Ej: 10, 25 — escribe 0 si aún no tienes)_"
                    )

                # ── Todos los campos obligatorios presentes → guardar ──
                etiqueta = producto_actual.get("etiqueta")  # puede ser None
                # Si etiqueta no fue mencionada en el formulario, preguntar
                if "etiqueta" not in campos:
                    datos_temp["inv_sub_paso"] = SUB_PASO_ETIQUETA
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"*{producto_actual['nombre']}* · talla {producto_actual['talla']} · "
                        f"S/ {producto_actual['precio_venta']:.2f} · {producto_actual['cantidad']} uds ✅\n\n"
                        "🏷️ ¿Le pones una *etiqueta* para reconocerlo fácil?\n"
                        "_(Ej: verano24, importado, outlet — o escribe *no* para omitir)_"
                    )

                # Tiene todo → insertar
                return await self._guardar_y_confirmar_producto(
                    negocio_id, datos_temp, producto_actual, etiqueta, productos_count
                )

            # ── Sub-paso: talla (fallback individual) ──
            elif sub_paso == SUB_PASO_TALLA:
                talla = mensaje.strip().upper()
                if not talla:
                    return "⚠️ La talla es obligatoria. ¿Cuál es la talla del producto?"
                producto_actual["talla"] = talla
                datos_temp["inv_producto_actual"] = producto_actual
                if not producto_actual.get("precio_venta"):
                    datos_temp["inv_sub_paso"] = SUB_PASO_PRECIO
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"Talla *{talla}* ✅\n\n"
                        "💰 ¿Cuál es el *precio de venta* en Soles?\n"
                        "_(Ej: 35, 49.90, 120)_"
                    )
                if producto_actual.get("cantidad") is None:
                    datos_temp["inv_sub_paso"] = SUB_PASO_STOCK
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"Talla *{talla}* · S/ {producto_actual['precio_venta']:.2f} ✅\n\n"
                        "📦 ¿Cuántas *unidades* tienes en stock?\n"
                        "_(Ej: 10, 25 — escribe 0 si no tienes)_"
                    )
                datos_temp["inv_sub_paso"] = SUB_PASO_ETIQUETA
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return (
                    "🏷️ ¿Le pones una *etiqueta*?\n"
                    "_(Ej: verano24, outlet — o escribe *no*)_"
                )

            # ── Sub-paso: precio (fallback individual) ──
            elif sub_paso == SUB_PASO_PRECIO:
                precio = self._parsear_precio(mensaje)
                if precio is None or precio <= 0:
                    return "⚠️ No pude leer el precio. Escríbelo en números, ej: _49.90_"
                producto_actual["precio_venta"] = precio
                datos_temp["inv_producto_actual"] = producto_actual
                if producto_actual.get("cantidad") is None:
                    datos_temp["inv_sub_paso"] = SUB_PASO_STOCK
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return (
                        f"Precio *S/ {precio:.2f}* ✅\n\n"
                        "📦 ¿Cuántas *unidades* tienes en stock?\n"
                        "_(Ej: 10, 25 — escribe 0 si no tienes)_"
                    )
                datos_temp["inv_sub_paso"] = SUB_PASO_ETIQUETA
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return (
                    "🏷️ ¿Le pones una *etiqueta*?\n"
                    "_(Ej: verano24, outlet — o escribe *no*)_"
                )

            # ── Sub-paso: stock (fallback individual) ──
            elif sub_paso == SUB_PASO_STOCK:
                cantidad = self._parsear_cantidad(mensaje)
                if cantidad is None:
                    return "⚠️ Escribe la cantidad en números, ej: _15_"
                producto_actual["cantidad"] = cantidad
                datos_temp["inv_producto_actual"] = producto_actual
                datos_temp["inv_sub_paso"] = SUB_PASO_ETIQUETA
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return (
                    f"Stock *{cantidad} uds* ✅\n\n"
                    "🏷️ ¿Le pones una *etiqueta* para reconocerlo fácil?\n"
                    "_(Ej: verano24, importado, outlet — o escribe *no* para omitir)_"
                )

            # ── Sub-paso: etiqueta ──
            elif sub_paso == SUB_PASO_ETIQUETA:
                etiqueta = None if self._es_omision(mensaje) else mensaje.strip()
                producto_actual["etiqueta"] = etiqueta
                datos_temp["inv_producto_actual"] = producto_actual
                return await self._guardar_y_confirmar_producto(
                    negocio_id, datos_temp, producto_actual, etiqueta, productos_count
                )

            # ── Sub-paso: continuar o terminar ──
            elif sub_paso == SUB_PASO_CONTINUAR:
                if any(p in msg_lower for p in ["otro", "más", "mas", "agregar", "sí", "si", "seguir", "continuar"]):
                    datos_temp["inv_sub_paso"] = SUB_PASO_NOMBRE
                    await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                    return self._mensaje_plantilla_producto(datos_temp.get("inv_productos_cargados", 0) + 1)

                elif self._detectar_confirmacion(msg_lower):
                    await self.upsert_sesion(negocio_id, "onboarding_6", datos_temp)
                    return (
                        f"¡Excelente! 🎉 Quedaron *{datos_temp.get('inv_productos_cargados', 0)}* "
                        f"producto(s) en tu inventario.\n\n"
                        "Última pregunta: ¿A qué hora *cierras tu tienda*?\n"
                        "_(Ej: 8pm, 20:00, 9 de la noche)_"
                    )
                else:
                    return (
                        "Escribe *otro* para agregar más productos "
                        "o *listo* para continuar con la configuración."
                    )

            # ── Sub-paso desconocido: reiniciar ──
            else:
                datos_temp["inv_sub_paso"] = SUB_PASO_NOMBRE
                datos_temp["inv_producto_actual"] = {}
                await self.upsert_sesion(negocio_id, "onboarding_5c", datos_temp)
                return self._mensaje_plantilla_producto(datos_temp.get("inv_productos_cargados", 0) + 1)

        # ══════════════════════════════════════════
        #  PASO 6 → Capturar horario de cierre
        # ══════════════════════════════════════════
        elif estado == "onboarding_6":
            horario = await gemini_service.extraer_dato(
                campo="hora de cierre en formato HH:MM",
                mensaje=mensaje,
            )
            datos_temp["horario_cierre"] = horario
            await self.actualizar_negocio(negocio_id, horario_cierre=horario)
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