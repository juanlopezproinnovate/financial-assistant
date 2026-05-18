import logging
import asyncio
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import get_pool
from app.services import ycloud

logger = logging.getLogger(__name__)

class SchedulerService:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=pytz.timezone("America/Lima"))

    def start(self):
        # Programar reporte de cierre para evaluar cada 30 minutos
        self.scheduler.add_job(
            self.enviar_reporte_cierre,
            CronTrigger(minute="0,30"),
            id="reporte_cierre_diario",
            replace_existing=True
        )
        self.scheduler.start()
        logger.info("🕒 Scheduler de tareas en segundo plano iniciado.")

    def stop(self):
        self.scheduler.shutdown()
        logger.info("🛑 Scheduler de tareas detenido.")

    async def enviar_reporte_cierre(self):
        """Envia el resumen del día y alerta de stock bajo a los negocios que cierran en este momento."""
        hoy_lima = datetime.now(pytz.timezone("America/Lima"))
        hora_actual = hoy_lima.strftime("%H:%M")
        
        logger.info(f"[Scheduler] Ejecutando tarea de reporte de cierre para horario: {hora_actual}...")
        pool = await get_pool()
        
        async with pool.acquire() as conn:
            # Obtener negocios activos cuyo horario de cierre coincide
            negocios = await conn.fetch(
                "SELECT id, telefono FROM negocios WHERE estado = 'activo' AND horario_cierre = $1", 
                hora_actual
            )
            
            if not negocios:
                logger.info(f"[Scheduler] Ningún negocio configurado para cerrar a las {hora_actual}.")
                return

            for negocio in negocios:
                negocio_id = str(negocio["id"])
                telefono = negocio["telefono"]
                
                try:
                    await self._generar_y_enviar_reporte(conn, negocio_id, telefono)
                except Exception as e:
                    logger.error(f"[Scheduler] Error al enviar reporte a {telefono}: {e}")

    async def _generar_y_enviar_reporte(self, conn, negocio_id, telefono):
        hoy_lima = datetime.now(pytz.timezone("America/Lima")).date()
        
        # 1. Obtener ventas y gastos del día actual
        resumen = await conn.fetchrow("""
            SELECT 
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'gasto'), 0) AS total_gastos,
                COUNT(id) FILTER (WHERE tipo = 'venta') AS num_ventas
            FROM transacciones 
            WHERE negocio_id = $1 AND fecha = $2
        """, negocio_id, hoy_lima)
        
        ventas = float(resumen["total_ventas"] or 0)
        gastos = float(resumen["total_gastos"] or 0)
        num_ventas = int(resumen["num_ventas"] or 0)
        ganancia = ventas - gastos

        # 2. Obtener productos con stock bajo (< 5)
        stock_bajo = await conn.fetch("""
            SELECT p.nombre, p.talla, s.cantidad
            FROM productos p
            JOIN stock s ON s.producto_id = p.id
            WHERE p.negocio_id = $1 AND p.activo = true AND s.cantidad < 5
            ORDER BY s.cantidad ASC
        """, negocio_id)

        # 3. Armar mensaje de reporte
        hoy_str = datetime.now(pytz.timezone("America/Lima")).strftime("%d/%m/%Y")
        
        mensaje = f"🌙 *Reporte de Cierre del Día* ({hoy_str})\n\n"
        mensaje += f"💰 *Ventas:* S/ {ventas:.2f} ({num_ventas} ventas)\n"
        mensaje += f"💸 *Gastos:* S/ {gastos:.2f}\n"
        
        emoji_ganancia = "📈" if ganancia >= 0 else "📉"
        mensaje += f"{emoji_ganancia} *Balance Diario:* S/ {ganancia:.2f}\n"

        if stock_bajo:
            mensaje += "\n⚠️ *ALERTA: STOCK BAJO* ⚠️\n"
            mensaje += "Los siguientes productos están por agotarse o se acabaron:\n"
            for p in stock_bajo:
                talla = f" (Talla {p['talla']})" if p['talla'] else ""
                cant = p['cantidad']
                emoji = "❌" if cant == 0 else "⚠️"
                mensaje += f" {emoji} {p['nombre']}{talla} - Quedan: *{cant}*\n"
        else:
            mensaje += "\n✅ _Todo tu inventario está sobre 5 unidades._\n"
            
        mensaje += "\n¡Que descanses! Nos vemos mañana. 😊"

        # Enviar WhatsApp
        await ycloud.send_text(telefono, mensaje)
        logger.info(f"[Scheduler] Reporte enviado exitosamente a {telefono}")

scheduler_service = SchedulerService()
