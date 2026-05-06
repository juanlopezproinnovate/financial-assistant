"""
app/services/onboarding_service.py
Flujo de onboarding para nuevos comerciantes — 4 pasos
Usa el pool de database.py existente.
"""

import json
import logging
from app.database import get_pool
from app.services.gemini_service import gemini_service

logger = logging.getLogger(__name__)


class OnboardingService:

    async def get_negocio(self, telefono: str) -> dict | None:
        """Obtiene el negocio del comerciante si ya completó onboarding."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM negocios WHERE telefono = $1", telefono
            )
        return dict(row) if row else None

    async def get_sesion(self, telefono: str) -> dict | None:
        """Obtiene el estado de sesión del usuario."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sesiones WHERE telefono = $1", telefono
            )
        return dict(row) if row else None

    async def upsert_sesion(self, telefono: str, estado: str, datos_temp: dict = None) -> None:
        """Crea o actualiza la sesión del usuario."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sesiones (telefono, estado, datos_temp, updated_at)
                VALUES ($1, $2, $3::jsonb, NOW())
                ON CONFLICT (telefono)
                DO UPDATE SET estado = $2, datos_temp = $3::jsonb, updated_at = NOW()
                """,
                telefono,
                estado,
                json.dumps(datos_temp or {}),
            )

    async def registrar_negocio(
        self, telefono: str, nombre: str, tipo_ropa: str, horario_cierre: str
    ) -> None:
        """Guarda el negocio configurado en la BD."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO negocios (telefono, nombre, tipo_ropa, horario_cierre, onboarding_completo, created_at)
                VALUES ($1, $2, $3, $4, true, NOW())
                ON CONFLICT (telefono)
                DO UPDATE SET
                    nombre = $2,
                    tipo_ropa = $3,
                    horario_cierre = $4,
                    onboarding_completo = true,
                    updated_at = NOW()
                """,
                telefono, nombre, tipo_ropa, horario_cierre,
            )
        logger.info(f"✅ Negocio registrado: {nombre} ({telefono})")

    def _leer_datos_temp(self, sesion: dict) -> dict:
        """Extrae datos_temp de la sesión de forma segura."""
        if not sesion:
            return {}
        raw = sesion.get("datos_temp", {})
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw or {}

    async def procesar(self, telefono: str, mensaje: str) -> str:
        """
        Punto de entrada principal. Determina el paso actual y responde.
        Retorna el texto a enviar por WhatsApp.
        """
        sesion = await self.get_sesion(telefono)
        estado = sesion.get("estado", "onboarding_1") if sesion else "onboarding_1"
        datos_temp = self._leer_datos_temp(sesion)

        logger.info(f"[Onboarding] {telefono} | estado={estado} | msg='{mensaje}'")

        # ── PASO 1: Primer contacto → bienvenida ──
        if not sesion or estado == "onboarding_1":
            result = await gemini_service.procesar_onboarding(paso=1)
            await self.upsert_sesion(telefono, "onboarding_2", {})
            return result.get("respuesta", "¡Hola! Soy Boti 👋 ¿Cómo se llama tu negocio?")

        # ── PASO 2: Guardar nombre → preguntar tipo de ropa ──
        elif estado == "onboarding_2":
            datos_temp["nombre"] = mensaje.strip().title()
            result = await gemini_service.procesar_onboarding(paso=2, mensaje_usuario=mensaje)
            await self.upsert_sesion(telefono, "onboarding_3", datos_temp)
            return result.get("respuesta", f"Perfecto 👌 ¿Qué tipo de ropa vendes? (dama, caballero, niños, todo)")

        # ── PASO 3: Guardar tipo ropa → preguntar horario ──
        elif estado == "onboarding_3":
            datos_temp["tipo_ropa"] = mensaje.strip().lower()
            result = await gemini_service.procesar_onboarding(paso=3, mensaje_usuario=mensaje)
            await self.upsert_sesion(telefono, "onboarding_4", datos_temp)
            return result.get("respuesta", "¿A qué hora cierras tu tienda? Te mando el resumen del día a esa hora 📊")

        # ── PASO 4: Guardar horario → completar onboarding ──
        elif estado == "onboarding_4":
            datos_temp["horario_cierre"] = mensaje.strip()
            await self.registrar_negocio(
                telefono,
                datos_temp.get("nombre", "Mi Negocio"),
                datos_temp.get("tipo_ropa", "ropa variada"),
                datos_temp.get("horario_cierre", "20:00"),
            )
            result = await gemini_service.procesar_onboarding(paso=4, mensaje_usuario=mensaje)
            await self.upsert_sesion(telefono, "activo", {})
            return result.get("respuesta", "¡Todo listo! 🎉 Prueba con: 'Vendí 2 polos a S/25 cada uno'")

        # Estado inesperado → reiniciar
        else:
            await self.upsert_sesion(telefono, "onboarding_1", {})
            return "¡Hola! Soy Boti. ¿Cómo se llama tu negocio? 😊"


onboarding_service = OnboardingService()