"""
app/services/onboarding_service.py
Adaptado al schema real del Día 1:
- negocios: whatsapp_numero, nombre_negocio, estado, onboarding_completo
- sesiones: negocio_id (FK), estado_conversacion, datos_temporales
"""

import json
import logging
from app.database import get_pool
from app.services.gemini_service import gemini_service

logger = logging.getLogger(__name__)


class OnboardingService:

    # ──────────────────────────────────────────────
    #  QUERIES — usan el schema real
    # ──────────────────────────────────────────────

    async def get_negocio(self, telefono: str) -> dict | None:
        """Busca negocio por whatsapp_numero."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM negocios WHERE whatsapp_numero = $1", telefono
            )
        return dict(row) if row else None

    async def get_sesion(self, telefono: str) -> dict | None:
        """Obtiene sesión via join con negocios."""
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
        """Crea o actualiza sesión por negocio_id."""
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
        """Crea registro inicial del negocio. Retorna el UUID."""
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
        """Actualiza campos del negocio por id."""
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
    #  HELPERS
    # ──────────────────────────────────────────────

    def _leer_datos_temp(self, sesion: dict) -> dict:
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
    #  FLUJO PRINCIPAL
    # ──────────────────────────────────────────────

    async def procesar(self, telefono: str, mensaje: str) -> str:
        """
        Punto de entrada. Determina el paso actual y responde.
        Retorna el texto a enviar por WhatsApp.
        """
        negocio = await self.get_negocio(telefono)

        # Si no existe, crear registro inicial
        if not negocio:
            negocio_id = await self.crear_negocio(telefono)
            await self.upsert_sesion(negocio_id, "onboarding_1", {})
            result = await gemini_service.procesar_onboarding(paso=1)
            return result.get("respuesta", "¡Hola! Soy Boti 👋 ¿Cómo se llama tu negocio?")

        negocio_id = str(negocio["id"])
        sesion = await self.get_sesion(telefono)
        estado = sesion.get("estado_conversacion", "onboarding_1") if sesion else "onboarding_1"
        datos_temp = self._leer_datos_temp(sesion)

        logger.info(f"[Onboarding] {telefono} | estado={estado} | msg='{mensaje}'")

        # ── PASO 1: Bienvenida ──
        if estado == "onboarding_1":
            result = await gemini_service.procesar_onboarding(paso=1)
            await self.upsert_sesion(negocio_id, "onboarding_2", {})
            return result.get("respuesta", "¡Hola! Soy Boti 👋 ¿Cómo se llama tu negocio?")

        # ── PASO 2: Guardar nombre → preguntar tipo de ropa ──
        elif estado == "onboarding_2":
            datos_temp["nombre_negocio"] = mensaje.strip().title()
            await self.actualizar_negocio(negocio_id, nombre_negocio=datos_temp["nombre_negocio"])
            result = await gemini_service.procesar_onboarding(paso=2, mensaje_usuario=mensaje)
            await self.upsert_sesion(negocio_id, "onboarding_3", datos_temp)
            return result.get("respuesta", "¿Qué tipo de ropa vendes? (dama, caballero, niños, todo)")

        # ── PASO 3: Guardar rubro → preguntar horario ──
        elif estado == "onboarding_3":
            datos_temp["rubro"] = mensaje.strip().lower()
            await self.actualizar_negocio(negocio_id, rubro=datos_temp["rubro"])
            result = await gemini_service.procesar_onboarding(paso=3, mensaje_usuario=mensaje)
            await self.upsert_sesion(negocio_id, "onboarding_4", datos_temp)
            return result.get("respuesta", "¿A qué hora cierras tu tienda? 📊")

        # ── PASO 4: Completar onboarding ──
        elif estado == "onboarding_4":
            datos_temp["horario_cierre"] = mensaje.strip()
            await self.completar_onboarding(negocio_id)
            result = await gemini_service.procesar_onboarding(paso=4, mensaje_usuario=mensaje)
            await self.upsert_sesion(negocio_id, "activo", {})
            return result.get("respuesta", "¡Todo listo! 🎉 Prueba: 'Vendí 2 polos a S/25 cada uno'")

        else:
            await self.upsert_sesion(negocio_id, "onboarding_1", {})
            return "¡Hola! Soy Boti. ¿Cómo se llama tu negocio? 😊"


onboarding_service = OnboardingService()