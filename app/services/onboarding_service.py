"""
app/services/onboarding_service.py

Flujo de onboarding completo:
  onboarding_1  → Bienvenida + capturar nombre del negocio
  onboarding_2  → Capturar nombre propio del dueño
  onboarding_3  → Capturar monedas aceptadas (PEN / CLP / ambas)
  onboarding_4  → Capturar tipo de ropa
  onboarding_5  → Proponer categorías y permitir ajustes hasta "listo"
  onboarding_6  → Capturar horario de cierre → completar onboarding

Cambios v2:
  - _leer_historial(): lee los últimos N pares del campo datos_temporales
  - guardar_en_historial(): persiste el par (user, assistant) en datos_temporales
    sin pisar otros campos como candidatos_stock, etc.

Schema usado:
  negocios  : whatsapp_numero, nombre_negocio, nombre_propietario, rubro,
              monedas_aceptadas, horario_cierre, estado, onboarding_completo
  sesiones  : negocio_id (FK), estado_conversacion, datos_temporales
  categorias: negocio_id (FK), nombre, tipo, color, activa
  categorias_plantilla: tipo_ropa, categorias (jsonb array)
"""

import json
import logging
from app.database import get_pool
from app.services.gemini_service import gemini_service

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Palabras clave que el cliente usa para confirmar que las categorías están ok
# ──────────────────────────────────────────────────────────────────────────────
PALABRAS_CONFIRMAR = {"listo", "ok", "bien", "perfecto", "dale", "sí", "si", "ya", "confirmar"}


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
                json.dumps(datos_temp or {}),
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
        """
        Busca categorías pre-generadas para ese tipo de ropa.
        Retorna lista de strings o None si no existe.
        """
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
        """
        Guarda o actualiza plantilla de categorías para este tipo de ropa.
        Si ya existe, incrementa el contador de uso.
        """
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
        """
        Inserta las categorías finales del negocio en la tabla `categorias`.
        Borra las anteriores primero para evitar duplicados.
        """
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
    #  HELPERS — datos temporales
    # ──────────────────────────────────────────────

    def _leer_datos_temp(self, sesion: dict | None) -> dict:
        """Deserializa datos_temporales de la sesión a dict."""
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
    #  HELPERS — historial de corto plazo
    # ──────────────────────────────────────────────

    def _leer_historial(self, sesion: dict | None) -> list[dict]:
        """
        Retorna la lista de mensajes anteriores guardados en datos_temporales.
        Formato: [{"role": "user"|"assistant", "content": str}, ...]
        """
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
        """
        Agrega el par (user, assistant) al historial dentro de datos_temporales
        y recorta a los últimos max_turns turnos (cada turno = 2 mensajes).

        Preserva todos los demás campos de datos_temporales (candidatos_stock,
        transaccion_id, etc.) — solo toca la clave 'historial_mensajes'.
        """
        # Leer datos_temp actuales para no pisar otros campos
        datos = self._leer_datos_temp(sesion)
        historial = datos.get("historial_mensajes", [])

        historial.append({"role": "user",      "content": user_msg})
        historial.append({"role": "assistant",  "content": bot_msg})

        # Mantener solo los últimos max_turns turnos
        max_mensajes = max_turns * 2
        if len(historial) > max_mensajes:
            historial = historial[-max_mensajes:]

        datos["historial_mensajes"] = historial

        # Persistir preservando el estado actual de la sesión
        estado_actual = (
            sesion.get("estado_conversacion", "activo") if sesion else "activo"
        )
        await self.upsert_sesion(negocio_id, estado_actual, datos)

    # ──────────────────────────────────────────────
    #  HELPERS — otros
    # ──────────────────────────────────────────────

    def _normalizar_tipo_ropa(self, texto: str) -> str:
        """Normaliza el tipo de ropa a minúsculas sin espacios extras."""
        return texto.strip().lower()

    def _detectar_confirmacion(self, mensaje: str) -> bool:
        """Retorna True si el mensaje es una confirmación del cliente."""
        return mensaje.strip().lower() in PALABRAS_CONFIRMAR

    def _formatear_lista_categorias(self, categorias: list[str]) -> str:
        """Convierte lista a texto numerado para mostrar por WhatsApp."""
        return "\n".join(f"{i+1}. {c}" for i, c in enumerate(categorias))

    def _filtrar_categorias_validas(self, categorias: list[str]) -> list[str]:
        """
        Permite SOLO prendas superiores e inferiores.
        Excluye calzado, accesorios, etc.
        """

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

            # match directo
            if c_norm in permitidas:
                categorias_validas.append(c.strip())
                continue

            # match parcial (clave 🔥 para cosas como "pantalon cargo", "polo oversize")
            for p in permitidas:
                if p in c_norm:
                    categorias_validas.append(c.strip())
                    break

        # eliminar duplicados manteniendo orden
        categorias_validas = list(dict.fromkeys(categorias_validas))

        # fallback si todo fue filtrado
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
                await self.upsert_sesion(negocio_id, "onboarding_6", datos_temp)
                return (
                    "¡Perfecto! 📦 Categorías guardadas.\n\n"
                    "Última pregunta: ¿A qué hora *cierras tu tienda*?\n"
                    "_(Ej: 8pm, 20:00, 9 de la noche)_"
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
            nombre_prop = datos_temp.get("nombre_propietario", "")
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