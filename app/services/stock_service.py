"""
app/services/stock_service.py

Orquesta el flujo completo de descuento de stock cuando se registra una venta:

  1. NLP ya extrajo: producto (str), cantidad, precio_unitario
  2. Buscar candidatos en `productos` del negocio via pg_trgm
  3. Resolver match con gemini_service.resolver_producto_venta()
  4. Según resultado:
     - exacto  → llamar a descontar_stock() en BD
     - parcial → guardar en sesión, preguntar al comerciante cuál es
     - ninguno → preguntar si crear el producto nuevo

El webhook.py llama a stock_service.procesar_venta() después de que
el intent VENTA ya fue detectado y la transacción ya fue insertada.
"""

import json
import logging
from app.database import get_pool
from app.services.gemini_service import gemini_service

logger = logging.getLogger(__name__)

# Umbral mínimo de similitud pg_trgm para considerar un candidato
SIMILITUD_MINIMA = 0.25
# Máximo de candidatos a evaluar (evita listas enormes al comerciante)
MAX_CANDIDATOS = 5


class StockService:

    # ──────────────────────────────────────────────
    #  QUERIES
    # ──────────────────────────────────────────────

    async def buscar_candidatos(
        self, negocio_id: str, nombre_buscado: str
    ) -> list[dict]:
        """
        Busca productos del negocio usando similitud pg_trgm.
        También busca en nombre_variantes (alias que el comerciante confirmó antes).
        Retorna lista ordenada por similitud DESC.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    p.id::text,
                    p.nombre,
                    p.talla,
                    p.nombre_variantes,
                    p.precio_venta_pen,
                    s.cantidad_actual,
                    s.cantidad_minima,
                    GREATEST(
                        similarity(p.nombre, $2),
                        COALESCE(
                            (SELECT MAX(similarity(v, $2))
                               FROM unnest(p.nombre_variantes) v),
                            0
                        )
                    ) AS similitud
                FROM productos p
                LEFT JOIN stock s ON s.producto_id = p.id
                WHERE p.negocio_id = $1
                  AND p.activo = true
                  AND GREATEST(
                        similarity(p.nombre, $2),
                        COALESCE(
                            (SELECT MAX(similarity(v, $2))
                               FROM unnest(p.nombre_variantes) v),
                            0
                        )
                      ) >= $3
                ORDER BY similitud DESC
                LIMIT $4
                """,
                negocio_id,
                nombre_buscado,
                SIMILITUD_MINIMA,
                MAX_CANDIDATOS,
            )
        return [dict(r) for r in rows]

    async def crear_producto(
        self,
        negocio_id: str,
        nombre: str,
        precio_venta: float | None,
        precio_costo: float | None,
        cantidad_inicial: int,
        talla: str | None = None,
        categoria_id: str | None = None,
    ) -> str:
        """
        Crea un producto nuevo en `productos` y su fila en `stock`.
        Retorna el UUID del producto creado.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO productos
                        (negocio_id, categoria_id, nombre, talla, precio_venta_pen, precio_costo, activo)
                    VALUES ($1, $2, $3, $4, $5, $6, true)
                    RETURNING id::text
                    """,
                    negocio_id,
                    categoria_id,
                    nombre.strip().title(),
                    talla.strip().upper() if talla else None,
                    precio_venta,
                    precio_costo,
                )
                producto_id = row["id"]

                await conn.execute(
                    """
                    INSERT INTO stock (producto_id, cantidad_actual, cantidad_minima)
                    VALUES ($1, $2, 5)
                    """,
                    producto_id,
                    max(0, cantidad_inicial),
                )

                # Registrar movimiento de entrada si hay stock inicial
                if cantidad_inicial > 0:
                    await conn.execute(
                        """
                        INSERT INTO stock_movimientos
                            (producto_id, negocio_id, tipo, cantidad,
                             cantidad_antes, cantidad_despues, motivo)
                        VALUES ($1, $2, 'entrada', $3, 0, $3, 'stock_inicial')
                        """,
                        producto_id,
                        negocio_id,
                        cantidad_inicial,
                    )

        logger.info(f"[Stock] Producto creado: '{nombre}' talla={talla} id={producto_id}")
        return producto_id

    async def vincular_producto_transaccion(
        self,
        transaccion_id: str,
        producto_id: str,
    ) -> None:
        """
        Actualiza el producto_id en una transacción ya registrada.
        También sincroniza la categoria_id desde el producto.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            producto = await conn.fetchrow(
                "SELECT categoria_id FROM productos WHERE id = $1",
                producto_id,
            )
            categoria_id = producto["categoria_id"] if producto else None
            await conn.execute(
                """
                UPDATE transacciones
                SET producto_id = $1, categoria_id = COALESCE($2, categoria_id)
                WHERE id = $3::uuid
                """,
                producto_id,
                categoria_id,
                transaccion_id,
            )
        logger.info(f"[Stock] Transacción {transaccion_id} vinculada a producto {producto_id}")

    async def agregar_variante(self, producto_id: str, variante: str) -> None:
        """
        Agrega un alias al array nombre_variantes del producto.
        Se llama cuando el comerciante confirma un match parcial
        para que la próxima vez sea match exacto.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE productos
                SET nombre_variantes = array_append(
                    COALESCE(nombre_variantes, ARRAY[]::varchar[]),
                    $2
                ),
                updated_at = NOW()
                WHERE id = $1
                  AND NOT ($2 = ANY(COALESCE(nombre_variantes, ARRAY[]::varchar[])))
                """,
                producto_id,
                variante.strip().lower(),
            )

    async def descontar_stock_bd(
        self,
        producto_id: str,
        negocio_id: str,
        cantidad: int,
        transaccion_id: str | None = None,
    ) -> dict:
        """
        Llama a la función PostgreSQL descontar_stock() que hace
        el UPDATE + INSERT en stock_movimientos de forma atómica.
        Retorna el jsonb resultado de la función.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT public.descontar_stock($1, $2, $3, $4, 'venta') AS resultado
                """,
                producto_id,
                negocio_id,
                cantidad,
                transaccion_id,
            )
        resultado = row["resultado"]
        if isinstance(resultado, str):
            resultado = json.loads(resultado)
        return dict(resultado)

    async def reponer_stock_bd(
        self,
        producto_id: str,
        negocio_id: str,
        cantidad: int,
        motivo: str = "reposicion",
    ) -> dict:
        """
        Llama a la función PostgreSQL reponer_stock() de forma atómica.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT public.reponer_stock($1, $2, $3, $4) AS resultado
                """,
                producto_id,
                negocio_id,
                cantidad,
                motivo,
            )
        resultado = row["resultado"]
        if isinstance(resultado, str):
            resultado = json.loads(resultado)
        return dict(resultado)

    async def get_producto(self, producto_id: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.*, s.cantidad_actual, s.cantidad_minima
                FROM productos p
                LEFT JOIN stock s ON s.producto_id = p.id
                WHERE p.id = $1
                """,
                producto_id,
            )
        return dict(row) if row else None

    # ──────────────────────────────────────────────
    #  FLUJO PRINCIPAL: procesar venta con stock
    # ──────────────────────────────────────────────

    async def procesar_venta(
        self,
        negocio_id: str,
        nombre_producto: str,
        cantidad: int,
        precio_unitario: float | None,
    ) -> dict:
        """
        SOLO hace matching del producto. NO registra transacción ni descuenta stock.
        El webhook decide qué hacer según el resultado.

        Retorna:
        {
          "estado": "exacto" | "parcial" | "sin_match",
          "producto_id": str | None,       — solo si exacto
          "producto_nombre": str | None,   — solo si exacto
          "producto_talla": str | None,    — solo si exacto
          "candidatos": [...] | None,      — solo si parcial
        }
        """
        logger.info(
            f"[Stock] procesar_venta (matching) negocio={negocio_id} "
            f"producto='{nombre_producto}' cant={cantidad}"
        )

        # 1. Buscar candidatos en el catálogo del negocio
        candidatos = await self.buscar_candidatos(negocio_id, nombre_producto)

        # 2. Resolver match con el modelo
        resolucion = await gemini_service.resolver_producto_venta(
            nombre_extraido=nombre_producto,
            candidatos=candidatos,
        )

        # ── CASO A: match exacto ──
        if resolucion["match"] == "exacto":
            producto_id = resolucion["producto_id"]
            producto = await self.get_producto(producto_id)
            return {
                "estado": "exacto",
                "producto_id": producto_id,
                "producto_nombre": producto["nombre"] if producto else nombre_producto,
                "producto_talla": producto.get("talla") if producto else None,
                "candidatos": None,
            }

        # ── CASO B: match parcial → lista de candidatos ──
        elif resolucion["match"] == "parcial":
            ids_candidatos = resolucion["candidatos_ids"]
            productos_info = [c for c in candidatos if c["id"] in ids_candidatos]
            # Si ninguno filtró (todos los candidatos), usar todos
            if not productos_info:
                productos_info = candidatos
            return {
                "estado": "parcial",
                "producto_id": None,
                "producto_nombre": None,
                "producto_talla": None,
                "candidatos": productos_info,
            }

        # ── CASO C: sin match ──
        else:
            return {
                "estado": "sin_match",
                "producto_id": None,
                "producto_nombre": None,
                "producto_talla": None,
                "candidatos": None,
            }

    async def ejecutar_descuento_venta(
        self,
        negocio_id: str,
        producto_id: str,
        nombre_producto: str,
        cantidad: int,
        transaccion_id: str | None = None,
    ) -> dict:
        """
        Ejecuta el descuento de stock una vez que el producto está resuelto.
        Aprende la variante si el nombre difiere. Actualiza categoria_id en la transacción.

        Retorna:
        {
          "ok": bool,
          "mensaje_stock": str,
          "alerta_stock": bool,
          "cantidad_restante": int | str,
          "producto_nombre": str,
          "producto_talla": str | None,
        }
        """
        try:
            resultado = await self.descontar_stock_bd(
                producto_id=producto_id,
                negocio_id=negocio_id,
                cantidad=cantidad,
                transaccion_id=transaccion_id,
            )
            producto = await self.get_producto(producto_id)

            # Aprender variante
            if producto and nombre_producto.lower() != producto["nombre"].lower():
                await self.agregar_variante(producto_id, nombre_producto)

            # Sincronizar categoria_id en la transacción
            if transaccion_id and producto and producto.get("categoria_id"):
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE transacciones SET categoria_id = $1 WHERE id = $2::uuid",
                        producto["categoria_id"], transaccion_id,
                    )

            alerta = resultado.get("alerta_stock", False)
            cantidad_restante = resultado.get("cantidad_despues", "?")
            p_nombre = producto["nombre"] if producto else nombre_producto
            p_talla = producto.get("talla") if producto else None
            str_talla = f" Talla {p_talla}" if p_talla else ""

            mensaje_stock = f"📦 Quedan {cantidad_restante} unidades de {p_nombre}{str_talla}."
            if alerta:
                mensaje_stock += f"\n⚠️ Stock bajo del mínimo ({resultado.get('cantidad_minima')} unid.)."

            return {
                "ok": True,
                "mensaje_stock": mensaje_stock,
                "alerta_stock": alerta,
                "cantidad_restante": cantidad_restante,
                "producto_nombre": p_nombre,
                "producto_talla": p_talla,
            }

        except Exception as e:
            logger.error(f"[Stock] Error descontando: {e}")
            prod = await self.get_producto(producto_id)
            p_nombre = prod["nombre"] if prod else nombre_producto
            p_talla = prod.get("talla") if prod else None
            if "insuficiente" in str(e).lower() or "check constraint" in str(e).lower():
                msg_error = "⚠️ No pude descontar el stock: no hay unidades suficientes registradas."
            else:
                msg_error = "⚠️ Hubo un problema al descontar el stock."
            return {
                "ok": False,
                "mensaje_stock": msg_error,
                "alerta_stock": False,
                "cantidad_restante": "?",
                "producto_nombre": p_nombre,
                "producto_talla": p_talla,
            }

    async def procesar_inventario(
        self,
        negocio_id: str,
        nombre_producto: str,
        cantidad: int,
        tipo: str,                        # "entrada" | "ajuste"
        precio_costo: float | None = None,
        precio_venta: float | None = None,
    ) -> dict:
        """
        Maneja el intent INVENTARIO: "llegaron 50 blusas" o "tengo 20 pantalones".
        - entrada  → reponer_stock_bd()
        - ajuste   → UPDATE directo de cantidad_actual (corrección)

        Igual que procesar_venta, primero resuelve el producto.
        """
        candidatos = await self.buscar_candidatos(negocio_id, nombre_producto)
        resolucion = await gemini_service.resolver_producto_venta(
            nombre_extraido=nombre_producto,
            candidatos=candidatos,
        )

        if resolucion["match"] == "exacto":
            producto_id = resolucion["producto_id"]

            if tipo == "entrada":
                resultado = await self.reponer_stock_bd(
                    producto_id=producto_id,
                    negocio_id=negocio_id,
                    cantidad=cantidad,
                    motivo="reposicion",
                )
                cantidad_nueva = resultado.get("cantidad_despues", "?")
                producto = await self.get_producto(producto_id)
                return {
                    "estado": "actualizado",
                    "mensaje": f"✅ Muy bien, se aumentaron {cantidad} unidades. En total hay {cantidad_nueva} unidades de {producto['nombre']}.",
                    "producto_id": producto_id,
                }

            else:  # ajuste
                pool = await get_pool()
                async with pool.acquire() as conn:
                    cantidad_antes = await conn.fetchval(
                        "SELECT cantidad_actual FROM stock WHERE producto_id = $1",
                        producto_id,
                    )
                    await conn.execute(
                        """
                        UPDATE stock
                        SET cantidad_actual = $1, ultima_actualizacion = NOW()
                        WHERE producto_id = $2
                        """,
                        cantidad,
                        producto_id,
                    )
                    diferencia = cantidad - (cantidad_antes or 0)
                    tipo_mov = "entrada" if diferencia >= 0 else "salida"
                    await conn.execute(
                        """
                        INSERT INTO stock_movimientos
                            (producto_id, negocio_id, tipo, cantidad,
                             cantidad_antes, cantidad_despues, motivo)
                        VALUES ($1, $2, $3, $4, $5, $6, 'ajuste_manual')
                        """,
                        producto_id,
                        negocio_id,
                        tipo_mov,
                        abs(diferencia),
                        cantidad_antes or 0,
                        cantidad,
                    )
                producto = await self.get_producto(producto_id)
                return {
                    "estado": "actualizado",
                    "mensaje": f"✅ Stock ajustado. {producto['nombre']} quedó en {cantidad} unidades.",
                    "producto_id": producto_id,
                }

        elif resolucion["match"] == "parcial":
            ids = resolucion["candidatos_ids"]
            productos_info = [c for c in candidatos if c["id"] in ids]
            lista = "\n".join(
                f"{i+1}. {p['nombre']}" + (f", Talla: {p['talla']}" if p.get("talla") else "") + f" (stock: {p.get('cantidad_actual', '?')})"
                for i, p in enumerate(productos_info)
            )
            return {
                "estado": "pendiente_seleccion",
                "mensaje": f"¿A cuál de estos te refieres?\n\n{lista}\n\nEscribe el número, o dime si no está en la lista.",
                "producto_id": None,
                "candidatos": productos_info,
            }

        else:
            # Producto nuevo: crear con stock inicial
            producto_id = await self.crear_producto(
                negocio_id=negocio_id,
                nombre=nombre_producto,
                precio_venta=precio_venta,
                precio_costo=precio_costo,
                cantidad_inicial=cantidad,
            )
            return {
                "estado": "creado",
                "mensaje": (
                    f'✅ Agregué "{nombre_producto.title()}" a tu catálogo '
                    f"con {cantidad} unidades en stock 📦"
                ),
                "producto_id": producto_id,
            }

    async def confirmar_seleccion_parcial(
        self,
        negocio_id: str,
        producto_id: str,
        nombre_original: str,
        cantidad: int,
        transaccion_id: str | None,
        operacion: str = "venta",         # "venta" | "inventario_entrada" | "inventario_ajuste"
    ) -> dict:
        """
        El comerciante eligió un número de la lista de candidatos.
        Ejecuta la operación pendiente y aprende la variante.
        """
        await self.agregar_variante(producto_id, nombre_original)

        if operacion == "venta":
            resultado = await self.descontar_stock_bd(
                producto_id=producto_id,
                negocio_id=negocio_id,
                cantidad=cantidad,
                transaccion_id=transaccion_id,
            )
            producto = await self.get_producto(producto_id)
            if transaccion_id and producto and producto.get("categoria_id"):
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE transacciones SET categoria_id = $1 WHERE id = $2::uuid",
                        producto["categoria_id"], transaccion_id
                    )

            alerta = resultado.get("alerta_stock", False)
            cantidad_restante = resultado.get("cantidad_despues", "?")
            
            str_talla = f" Talla {producto['talla']}" if producto.get("talla") else ""
            mensaje = f"📦 Listo. Quedan {cantidad_restante} unidades de {producto['nombre']}{str_talla}."
            if alerta:
                mensaje += f"\n⚠️ Stock bajo del mínimo."
            return {
                "estado": "descontado", 
                "mensaje": mensaje, 
                "alerta_stock": alerta,
                "producto_nombre": producto["nombre"],
                "producto_talla": producto.get("talla"),
            }

        elif operacion.startswith("inventario_"):
            # Determinar motivo
            motivo = "reposicion" if "entrada" in operacion else "ajuste"
            resultado = await self.reponer_stock_bd(
                producto_id=producto_id,
                negocio_id=negocio_id,
                cantidad=cantidad,
                motivo=motivo,
            )
            producto = await self.get_producto(producto_id)
            cantidad_final = resultado.get('cantidad_despues', '?')
            return {
                "estado": "actualizado",
                "mensaje": f"✅ Muy bien, se aumentaron {cantidad} unidades. En total hay {cantidad_final} unidades de {producto['nombre']}.",
            }

        return {"estado": "ok", "mensaje": f"✅ Operación completada. ({operacion})"}


stock_service = StockService()