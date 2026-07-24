import logging
import asyncio
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import get_pool
from app.services import ycloud

logger = logging.getLogger(__name__)

class SchedulerService:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=pytz.timezone("America/Lima"))

    def start(self):
        # Job existente: reporte de cierre cada 30 minutos (DESACTIVADO TEMPORALMENTE)
        # self.scheduler.add_job(
        #     self.enviar_reporte_cierre,
        #     CronTrigger(minute="0,30"),
        #     id="reporte_cierre_diario",
        #     replace_existing=True,
        # )

        # ── NUEVO: revisar recordatorios personalizados cada minuto ──
        self.scheduler.add_job(
            self.enviar_recordatorios_pendientes,
            IntervalTrigger(minutes=1),
            id="recordatorios_personalizados",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info("🕒 Scheduler de tareas en segundo plano iniciado.")

    def stop(self):
        if self.scheduler.state != STATE_STOPPED:
            self.scheduler.shutdown()
        logger.info("🛑 Scheduler de tareas detenido.")

    # ──────────────────────────────────────────────────────
    #  NUEVO: Recordatorios personalizados
    # ──────────────────────────────────────────────────────

    async def enviar_recordatorios_pendientes(self):
        """
        Revisa cada minuto los recordatorios personalizados pendientes
        cuya fecha_hora ya llegó y los envía por WhatsApp.
        """
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT r.id, r.mensaje, r.fecha_hora,
                           n.whatsapp_numero, n.nombre_propietario,
                           n.zona_horaria
                    FROM recordatorios r
                    JOIN negocios n ON n.id = r.negocio_id
                    WHERE r.estado = 'pendiente'
                      AND r.tipo = 'personalizado'
                      AND r.fecha_hora <= NOW()
                    """,
                )

                if not rows:
                    return

                for row in rows:
                    try:
                        nombre     = row["nombre_propietario"] or "Comerciante"
                        zona_str   = row["zona_horaria"] or "America/Lima"
                        tz         = pytz.timezone(zona_str)
                        hora_local = row["fecha_hora"].astimezone(tz)
                        hora_fmt   = hora_local.strftime("%I:%M %p").lstrip("0")

                        texto = (
                            f"🔔 {nombre}, son las {hora_fmt}.\n"
                            f"{row['mensaje']}"
                        )

                        await conn.execute(
                            """
                            UPDATE recordatorios
                            SET estado = 'enviado', enviado_at = NOW()
                            WHERE id = $1
                            """,
                            row["id"],
                        )

                        await ycloud.send_text(row["whatsapp_numero"], texto)
                        logger.info(
                            f"[Scheduler] Recordatorio enviado a {row['whatsapp_numero']} "
                            f"| id={row['id']}"
                        )

                    except Exception as e:
                        logger.error(
                            f"[Scheduler] Error enviando recordatorio {row['id']}: {e}"
                        )

        except Exception as e:
            logger.error(f"[Scheduler] Error revisando recordatorios: {e}")

    # ──────────────────────────────────────────────────────
    #  EXISTENTE: Reporte de cierre diario
    # ──────────────────────────────────────────────────────

    async def enviar_reporte_cierre(self):
        """Envía el resumen del día a los negocios que cierran en este momento."""
        hoy_lima    = datetime.now(pytz.timezone("America/Lima"))
        hora_actual = hoy_lima.strftime("%H:%M")

        logger.info(f"[Scheduler] Ejecutando reporte de cierre para horario: {hora_actual}...")
        pool = await get_pool()

        async with pool.acquire() as conn:
            negocios = await conn.fetch(
                "SELECT id, whatsapp_numero FROM negocios WHERE estado = 'activo' AND horario_cierre = $1",
                hora_actual,
            )

            if not negocios:
                logger.info(f"[Scheduler] Ningún negocio configurado para cerrar a las {hora_actual}.")
                return

            for negocio in negocios:
                negocio_id = str(negocio["id"])
                telefono   = negocio["whatsapp_numero"]
                try:
                    await self._generar_y_enviar_reporte(conn, negocio_id, telefono)
                except Exception as e:
                    logger.error(f"[Scheduler] Error al enviar reporte a {telefono}: {e}")

    async def _generar_y_enviar_reporte(self, conn, negocio_id, telefono):
        hoy_lima = datetime.now(pytz.timezone("America/Lima")).date()

        resumen = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'gasto'), 0) AS total_gastos,
                COUNT(id) FILTER (WHERE tipo = 'venta') AS num_ventas
            FROM transacciones
            WHERE negocio_id = $1 AND fecha = $2
            """,
            negocio_id,
            hoy_lima,
        )

        ventas    = float(resumen["total_ventas"] or 0)
        gastos    = float(resumen["total_gastos"] or 0)
        num_ventas = int(resumen["num_ventas"] or 0)
        ganancia  = ventas - gastos

        stock_bajo = await conn.fetch(
            """
            SELECT p.nombre, p.talla, s.cantidad_actual AS cantidad
            FROM productos p
            JOIN stock s ON s.producto_id = p.id
            WHERE p.negocio_id = $1 AND p.activo = true AND s.cantidad_actual < 5
            ORDER BY s.cantidad_actual ASC
            """,
            negocio_id,
        )

        hoy_str = datetime.now(pytz.timezone("America/Lima")).strftime("%d/%m/%Y")

        mensaje  = f"🌙 *Reporte de Cierre del Día* ({hoy_str})\n\n"
        mensaje += f"💰 *Ventas:* S/ {ventas:.2f} ({num_ventas} ventas)\n"
        mensaje += f"💸 *Gastos:* S/ {gastos:.2f}\n"
        emoji_ganancia = "📈" if ganancia >= 0 else "📉"
        mensaje += f"{emoji_ganancia} *Balance Diario:* S/ {ganancia:.2f}\n"

        if stock_bajo:
            mensaje += "\n⚠️ *ALERTA: STOCK BAJO* ⚠️\n"
            mensaje += "Los siguientes productos están por agotarse:\n"
            for p in stock_bajo:
                talla = f" (Talla {p['talla']})" if p["talla"] else ""
                cant  = p["cantidad"]
                emoji = "❌" if cant == 0 else "⚠️"
                mensaje += f" {emoji} {p['nombre']}{talla} - Quedan: *{cant}*\n"
        else:
            mensaje += "\n✅ _Todo tu inventario está sobre 5 unidades._\n"

        mensaje += "\n¡Que descanses! Nos vemos mañana. 😊"

        await ycloud.send_text(telefono, mensaje)
        logger.info(f"[Scheduler] Reporte de cierre enviado a {telefono}")


scheduler_service = SchedulerService()