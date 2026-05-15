"""
app/routers/webhook.py

Cambios v2:
  - _process_text() lee el historial de corto plazo antes de llamar a Groq
    y lo pasa a gemini_service.procesar_mensaje().
  - Nuevo intent INCOMPLETO: el bot pregunta el dato faltante y guarda el
    par (user, assistant) en el historial para que el siguiente mensaje
    tenga contexto.
  - Todos los intents que generan una respuesta final también guardan el
    par en el historial, para que mensajes posteriores de aclaración funcionen.
"""
import os
import logging
import datetime
from fastapi import APIRouter, Request, Header, HTTPException, status
from app.config import settings
from app.services import ycloud
from app.services.gemini_service import gemini_service
from app.services.onboarding_service import onboarding_service
from app.services.stock_service import stock_service
from app.services.groq_service import procesar_audio_whatsapp
from app.database import get_pool
from app.services.polly_service import polly_service
from app.services.storage_service import upload_audio_to_supabase
import uuid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_token(token: str | None) -> None:
    if not settings.YCLOUD_WEBHOOK_TOKEN:
        return
    if token != settings.YCLOUD_WEBHOOK_TOKEN:
        logger.warning(f"Token inválido recibido: {token!r}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


@router.post("")
async def receive_event(
    request: Request,
    x_ycloud_webhook_token: str | None = Header(default=None),
):
    _verify_token(x_ycloud_webhook_token)

    body = await request.json()
    event_type: str = body.get("type", "")
    logger.info(f"📥 Evento YCloud: {event_type}")

    if event_type == "whatsapp.inbound_message.received":
        await _handle_inbound(body)

    elif event_type == "whatsapp.message.updated":
        wa_msg = body.get("whatsappMessage", {})
        logger.info(f"📊 Estado | id={wa_msg.get('id')} | status={wa_msg.get('status')}")

    else:
        logger.debug(f"Evento ignorado: {event_type}")

    return {"status": "ok"}


async def _handle_inbound(body: dict) -> None:
    logger.info(f"[RAW BODY*] {body}")
    msg: dict = body.get("whatsappInboundMessage", {})
    from_number: str = msg.get("from", "")
    msg_type: str = msg.get("type", "")

    logger.info(f"📨 Mensaje de {from_number} | tipo={msg_type}")

    if not from_number:
        logger.warning("Webhook sin 'from' — ignorado")
        return

    if msg_type == "text":
        text: str = msg.get("text", {}).get("body", "").strip()
        logger.info(f"   Texto: {text!r}")
        await _process_text(from_number, text, es_audio=False)

    elif msg_type == "audio":
        await _process_audio(from_number, msg)

    elif msg_type == "image":
        await ycloud.send_text(from_number, "🖼️ Recibí tu imagen.")

    else:
        logger.info(f"   Tipo no manejado aún: {msg_type}")


async def _process_audio(from_number: str, msg: dict) -> None:
    await ycloud.send_text(from_number, "🎙️ Escuchando tu audio...")

    logger.info(f"[Audio] msg completo: {msg}")
    audio_data = msg.get("audio", {})
    audio_url  = audio_data.get("link", "")
    mime_type  = audio_data.get("mime_type", "audio/ogg")

    logger.info(f"[Audio] url='{audio_url}' | mime='{mime_type}'")

    if not audio_url:
        await ycloud.send_text(from_number, "No pude acceder al audio. ¿Puedes escribirlo? ✍️")
        return

    texto_transcrito = await procesar_audio_whatsapp(audio_url, mime_type)

    if not texto_transcrito:
        await ycloud.send_text(
            from_number,
            "No entendí el audio 😅 ¿Puedes escribirlo o intentar de nuevo?"
        )
        return

    logger.info(f"[Audio→Texto] {from_number}: '{texto_transcrito}'")
    await ycloud.send_text(from_number, f"🎙️ Escuché: «{texto_transcrito}»")
    await _process_text(from_number, texto_transcrito, es_audio=True)


async def _process_text(from_number: str, text: str, es_audio: bool = False) -> None:
    try:
        negocio = await onboarding_service.get_negocio(from_number)
        en_onboarding = not negocio or not negocio.get("onboarding_completo")

        # ── FLUJO A: Onboarding ──
        if en_onboarding:
            respuesta = await onboarding_service.procesar(from_number, text)
            await ycloud.send_text(from_number, respuesta)
            if es_audio:
                await _responder_con_voz(from_number, respuesta)
            return

        # ── Datos comunes ──
        sesion         = await onboarding_service.get_sesion(from_number)
        estado         = sesion.get("estado_conversacion", "activo") if sesion else "activo"
        datos_temp     = onboarding_service._leer_datos_temp(sesion)
        negocio_id_str = str(negocio["id"])

        # ── FLUJO B1: Estados de edición / eliminación de transacciones ──
        if estado in ["ESPERANDO_SELECCION_ELIMINAR", "ESPERANDO_SELECCION_EDITAR"]:
            if text.lower().strip() == "cancelar":
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                await ycloud.send_text(from_number, "Operación cancelada. ¿En qué más te ayudo? 😊")
                return

            try:
                seleccion = int(text.strip())
                ultimas   = datos_temp.get("ultimas_transacciones", [])
                if 1 <= seleccion <= len(ultimas):
                    tx      = ultimas[seleccion - 1]
                    simbolos = {"PEN": "S/", "USD": "$", "BOB": "Bs.", "CLP": "CLP"}
                    simbolo  = simbolos.get(tx["moneda"], tx["moneda"])

                    if estado == "ESPERANDO_SELECCION_ELIMINAR":
                        await _eliminar_transaccion(tx["id"])
                        await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                        await ycloud.send_text(
                            from_number,
                            f"✅ Transacción eliminada: {tx['descripcion']} ({simbolo} {tx['monto']:.2f})",
                        )
                        return

                    else:  # ESPERANDO_SELECCION_EDITAR
                        datos_temp["transaccion_a_editar"] = tx
                        await onboarding_service.upsert_sesion(
                            negocio_id_str, "ESPERANDO_EDICION_TRANSACCION", datos_temp
                        )
                        await ycloud.send_text(
                            from_number,
                            f"Elegiste: {tx['descripcion']} ({simbolo} {tx['monto']:.2f}).\n"
                            f"¿Qué deseas cambiar? (ej. 'cambia el monto a 50' o 'cancelar')",
                        )
                        return
                else:
                    await ycloud.send_text(from_number, "Por favor envía un número válido de la lista o 'cancelar'.")
                    return
            except ValueError:
                await ycloud.send_text(from_number, "Por favor envía un número o la palabra 'cancelar'.")
                return

        elif estado == "ESPERANDO_EDICION_TRANSACCION":
            if text.lower().strip() == "cancelar":
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                await ycloud.send_text(from_number, "Edición cancelada. ¿En qué más te ayudo? 😊")
                return

            tx = datos_temp.get("transaccion_a_editar")
            if not tx:
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                await ycloud.send_text(from_number, "Ocurrió un error. Operación cancelada.")
                return

            cambios = await gemini_service.interpretar_edicion(tx, text)
            if cambios:
                await _actualizar_transaccion(tx["id"], cambios)
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                cambios_str = ", ".join(f"{k}: {v}" for k, v in cambios.items())
                await ycloud.send_text(
                    from_number,
                    f"✅ Transacción actualizada correctamente.\nCambios aplicados: {cambios_str}",
                )
            else:
                await ycloud.send_text(
                    from_number,
                    "No entendí qué cambiar 🤔. Intenta decirlo de otra forma o escribe 'cancelar'.",
                )
            return

        # ── FLUJO B2: Estado ESPERANDO_SELECCION_STOCK ──
        elif estado == "ESPERANDO_SELECCION_STOCK":
            candidatos  = datos_temp.get("candidatos_stock", [])
            operacion   = datos_temp.get("operacion_stock", "venta")
            cantidad    = datos_temp.get("cantidad_stock", 1)
            nombre_orig = datos_temp.get("nombre_producto_original", "")
            msg_lower   = text.strip().lower()

            # Cancelar: registrar venta sin stock
            if msg_lower == "cancelar":
                if operacion == "venta":
                    simbolo_s = datos_temp.get("venta_moneda", "S/")
                    tx_id = await _guardar_transaccion_retornando_id(
                        negocio_id  = negocio_id_str,
                        tipo        = "venta",
                        descripcion = nombre_orig,
                        monto       = datos_temp.get("venta_monto", 0),
                        moneda      = datos_temp.get("venta_moneda_codigo", "PEN"),
                        fecha       = datos_temp.get("venta_fecha"),
                        hora        = datos_temp.get("venta_hora"),
                        cantidad    = cantidad,
                    )
                    nombre_propio = datos_temp.get("venta_nombre_propio", "Comerciante")
                    conf = (
                        f"✅ Venta registrada, {nombre_propio}\n\n"
                        f"📅 {datos_temp.get('venta_fecha','')} {datos_temp.get('venta_hora','')}\n"
                        f"📝 Producto: {nombre_orig}\n"
                        f"📦 Cantidad: {cantidad}\n"
                        f"💰 Total: {simbolo_s} {datos_temp.get('venta_monto', 0):.2f}\n\n"
                        f"_(Sin descuento de stock)_"
                    )
                    await ycloud.send_text(from_number, conf)
                else:
                    await ycloud.send_text(from_number, "Cancelado. El stock no fue modificado.")
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                return

            # "0" o "ninguno": pasar a decisión de producto nuevo
            if msg_lower in ["0", "ninguno", "ninguna", "no está", "no esta", "no está aquí", "no esta aqui"]:
                if operacion == "venta":
                    msg_decision = (
                        f"Entendido, no está en la lista 🔍\n\n"
                        f"¿Qué quieres hacer?\n"
                        f"1️⃣ *Agregar* al catálogo\n"
                        f"2️⃣ *Seguir* sin añadir al stock\n\n"
                        f"_(La venta se registrará cuando elijas)_"
                    )
                    await onboarding_service.upsert_sesion(
                        negocio_id_str, "ESPERANDO_DECISION_PRODUCTO_NUEVO", datos_temp
                    )
                    await ycloud.send_text(from_number, msg_decision)
                else:
                    await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                    await ycloud.send_text(from_number, "Operación cancelada. ¿En qué más te ayudo?")
                return

            # Número de selección
            try:
                seleccion = int(text.strip())
                if 1 <= seleccion <= len(candidatos):
                    producto_id_sel = candidatos[seleccion - 1]["id"]

                    if operacion == "venta":
                        prod = await stock_service.get_producto(producto_id_sel)
                        prod_nombre = prod["nombre"] if prod else nombre_orig
                        prod_talla  = prod.get("talla") if prod else None
                        nombre_final = prod_nombre + (f" Talla {prod_talla}" if prod_talla else "")

                        # Registrar transacción ahora
                        tx_id = await _guardar_transaccion_retornando_id(
                            negocio_id  = negocio_id_str,
                            tipo        = "venta",
                            descripcion = nombre_final,
                            monto       = datos_temp.get("venta_monto", 0),
                            moneda      = datos_temp.get("venta_moneda_codigo", "PEN"),
                            fecha       = datos_temp.get("venta_fecha"),
                            hora        = datos_temp.get("venta_hora"),
                            cantidad    = cantidad,
                            producto_id = producto_id_sel,
                        )

                        # Descontar stock
                        descuento = await stock_service.ejecutar_descuento_venta(
                            negocio_id      = negocio_id_str,
                            producto_id     = producto_id_sel,
                            nombre_producto = nombre_orig,
                            cantidad        = cantidad,
                            transaccion_id  = tx_id,
                        )

                        await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                        simbolo_s    = datos_temp.get("venta_moneda", "S/")
                        nombre_propio = datos_temp.get("venta_nombre_propio", "Comerciante")
                        conf = (
                            f"✅ Venta registrada, {nombre_propio}\n\n"
                            f"📅 {datos_temp.get('venta_fecha','')} {datos_temp.get('venta_hora','')}\n"
                            f"📝 Producto: {nombre_final}\n"
                            f"📦 Cantidad: {cantidad}\n"
                            f"💰 Total: {simbolo_s} {datos_temp.get('venta_monto', 0):.2f}"
                        )
                        await ycloud.send_text(from_number, conf)
                        await ycloud.send_text(from_number, descuento["mensaje_stock"])

                    else:
                        # Inventario — usar flujo existente
                        resultado = await stock_service.confirmar_seleccion_parcial(
                            negocio_id      = negocio_id_str,
                            producto_id     = producto_id_sel,
                            nombre_original = nombre_orig,
                            cantidad        = cantidad,
                            transaccion_id  = None,
                            operacion       = operacion,
                        )
                        await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                        await ycloud.send_text(from_number, resultado["mensaje"])
                else:
                    await ycloud.send_text(
                        from_number,
                        f"Escribe un número entre 1 y {len(candidatos)}, *0* si no está en la lista, o *cancelar*.",
                    )
            except ValueError:
                await ycloud.send_text(
                    from_number,
                    f"Escribe el número del producto, *0* si no está aquí, o *cancelar*."
                )
            return

        # ── FLUJO B3: Estado ESPERANDO_DECISION_PRODUCTO_NUEVO ──
        elif estado == "ESPERANDO_DECISION_PRODUCTO_NUEVO":
            nombre_prod     = datos_temp.get("nombre_producto_original", "")
            cantidad        = datos_temp.get("cantidad_stock", 1)
            precio_unitario = datos_temp.get("precio_unitario_stock")

            decision = await gemini_service.interpretar_decision_producto_nuevo(text)
            accion   = decision["accion"]

            if accion == "AGREGAR":
                # Extraer nombre limpio + talla
                separado = await gemini_service.extraer_nombre_y_talla(nombre_prod)
                nombre_limpio = separado.get("nombre") or nombre_prod.strip().title()
                talla_extraida = separado.get("talla")

                precio_venta_fmt = f"S/ {precio_unitario:.2f}" if precio_unitario else "(no especificado)"
                formulario = {
                    "nombre":        nombre_limpio,
                    "talla":         talla_extraida,
                    "stock":         None,
                    "precio_venta":  precio_unitario,
                    "precio_compra": None,
                }
                datos_temp["formulario_producto"] = formulario

                talla_linea = f"📐 *Talla:* {talla_extraida}\n" if talla_extraida else "📐 *Talla:* (no especificada — puedes agregarla)\n"
                msg_form = (
                    f"Cuéntame más sobre el producto 📦\n\n"
                    f"📝 *Nombre:* {nombre_limpio}\n"
                    f"{talla_linea}"
                    f"📦 *Stock:* (¿cuántas unidades tienes ahora?)\n"
                    f"💰 *Precio de Venta:* {precio_venta_fmt}\n"
                    f"💵 *Precio de Compra:* (opcional)\n\n"
                    f"Edita lo que quieras en un mensaje, escribe *guardar* para confirmar, o *cancelar*."
                )
                await onboarding_service.upsert_sesion(
                    negocio_id_str, "ESPERANDO_DATOS_PRODUCTO_NUEVO", datos_temp
                )
                await ycloud.send_text(from_number, msg_form)

            elif accion in ("CONTINUAR", "CANCELAR"):
                # Registrar venta sin producto_id
                simbolo_s     = datos_temp.get("venta_moneda", "S/")
                nombre_propio = datos_temp.get("venta_nombre_propio", "Comerciante")
                tx_id = await _guardar_transaccion_retornando_id(
                    negocio_id  = negocio_id_str,
                    tipo        = "venta",
                    descripcion = nombre_prod,
                    monto       = datos_temp.get("venta_monto", 0),
                    moneda      = datos_temp.get("venta_moneda_codigo", "PEN"),
                    fecha       = datos_temp.get("venta_fecha"),
                    hora        = datos_temp.get("venta_hora"),
                    cantidad    = cantidad,
                )
                conf = (
                    f"✅ Venta registrada, {nombre_propio}\n\n"
                    f"📅 {datos_temp.get('venta_fecha','')} {datos_temp.get('venta_hora','')}\n"
                    f"📝 Producto: {nombre_prod}\n"
                    f"📦 Cantidad: {cantidad}\n"
                    f"💰 Total: {simbolo_s} {datos_temp.get('venta_monto', 0):.2f}\n\n"
                    f"_(Sin descuento de stock)_"
                )
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                await ycloud.send_text(from_number, conf)

            else:
                await ycloud.send_text(
                    from_number,
                    "No entendí 😅\nEscribe *1* para *Agregar* al catálogo o *2* para *Seguir* sin stock."
                )
            return

        # ── FLUJO B4: Estado ESPERANDO_DATOS_PRODUCTO_NUEVO ──
        elif estado == "ESPERANDO_DATOS_PRODUCTO_NUEVO":
            formulario      = datos_temp.get("formulario_producto", {})
            nombre_prod     = datos_temp.get("nombre_producto_original", "")
            cantidad        = datos_temp.get("cantidad_stock", 1)

            interpretacion = await gemini_service.interpretar_formulario_producto_venta(text, formulario)
            accion  = interpretacion["accion"]
            cambios = interpretacion.get("cambios", {})

            if accion == "EDITAR":
                # Aplicar cambios al formulario
                for campo, valor in cambios.items():
                    if valor is not None:
                        formulario[campo] = valor
                datos_temp["formulario_producto"] = formulario

                talla_linea = f"📐 *Talla:* {formulario.get('talla')}\n" if formulario.get("talla") else "📐 *Talla:* (no especificada)\n"
                pv = formulario.get("precio_venta")
                pc = formulario.get("precio_compra")
                st = formulario.get("stock")
                msg_form = (
                    f"📝 *Nombre:* {formulario.get('nombre', '?')}\n"
                    f"{talla_linea}"
                    f"📦 *Stock:* {st if st is not None else '(no especificado)'}\n"
                    f"💰 *Precio de Venta:* {f'S/ {pv:.2f}' if pv else '(no especificado)'}\n"
                    f"💵 *Precio de Compra:* {f'S/ {pc:.2f}' if pc else '(opcional)'}\n\n"
                    f"¿Queda así? Escribe *guardar*, sigue editando, o *cancelar*."
                )
                await onboarding_service.upsert_sesion(
                    negocio_id_str, "ESPERANDO_DATOS_PRODUCTO_NUEVO", datos_temp
                )
                await ycloud.send_text(from_number, msg_form)

            elif accion == "GUARDAR":
                # Validar campos mínimos
                if not formulario.get("nombre"):
                    await ycloud.send_text(from_number, "Falta el nombre del producto. ¿Cómo se llama?")
                    return
                if formulario.get("stock") is None:
                    await ycloud.send_text(from_number, "¿Cuántas unidades tienes en stock ahora?")
                    return

                # Crear producto
                producto_id_nuevo = await stock_service.crear_producto(
                    negocio_id      = negocio_id_str,
                    nombre          = formulario["nombre"],
                    talla           = formulario.get("talla"),
                    precio_venta    = formulario.get("precio_venta"),
                    precio_costo    = formulario.get("precio_compra"),
                    cantidad_inicial= formulario["stock"],
                )

                # Registrar transacción con producto_id
                nombre_final = formulario["nombre"] + (f" Talla {formulario['talla']}" if formulario.get("talla") else "")
                simbolo_s     = datos_temp.get("venta_moneda", "S/")
                nombre_propio = datos_temp.get("venta_nombre_propio", "Comerciante")
                tx_id = await _guardar_transaccion_retornando_id(
                    negocio_id  = negocio_id_str,
                    tipo        = "venta",
                    descripcion = nombre_final,
                    monto       = datos_temp.get("venta_monto", 0),
                    moneda      = datos_temp.get("venta_moneda_codigo", "PEN"),
                    fecha       = datos_temp.get("venta_fecha"),
                    hora        = datos_temp.get("venta_hora"),
                    cantidad    = cantidad,
                    producto_id = producto_id_nuevo,
                )

                # Descontar stock de la venta
                descuento = await stock_service.ejecutar_descuento_venta(
                    negocio_id      = negocio_id_str,
                    producto_id     = producto_id_nuevo,
                    nombre_producto = nombre_prod,
                    cantidad        = cantidad,
                    transaccion_id  = tx_id,
                )

                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})

                conf_venta = (
                    f"✅ Venta registrada, {nombre_propio}\n\n"
                    f"📅 {datos_temp.get('venta_fecha','')} {datos_temp.get('venta_hora','')}\n"
                    f"📝 Producto: {nombre_final}\n"
                    f"📦 Cantidad: {cantidad}\n"
                    f"💰 Total: {simbolo_s} {datos_temp.get('venta_monto', 0):.2f}"
                )
                conf_producto = (
                    f"✅ \"{nombre_final}\" agregado a tu catálogo 📦\n"
                    f"{descuento['mensaje_stock']}"
                )
                await ycloud.send_text(from_number, conf_venta)
                await ycloud.send_text(from_number, conf_producto)

            elif accion == "CANCELAR":
                # Registrar venta sin producto_id
                simbolo_s     = datos_temp.get("venta_moneda", "S/")
                nombre_propio = datos_temp.get("venta_nombre_propio", "Comerciante")
                tx_id = await _guardar_transaccion_retornando_id(
                    negocio_id  = negocio_id_str,
                    tipo        = "venta",
                    descripcion = nombre_prod,
                    monto       = datos_temp.get("venta_monto", 0),
                    moneda      = datos_temp.get("venta_moneda_codigo", "PEN"),
                    fecha       = datos_temp.get("venta_fecha"),
                    hora        = datos_temp.get("venta_hora"),
                    cantidad    = cantidad,
                )
                conf = (
                    f"✅ Venta registrada, {nombre_propio}\n\n"
                    f"📅 {datos_temp.get('venta_fecha','')} {datos_temp.get('venta_hora','')}\n"
                    f"📝 Producto: {nombre_prod}\n"
                    f"📦 Cantidad: {cantidad}\n"
                    f"💰 Total: {simbolo_s} {datos_temp.get('venta_monto', 0):.2f}\n\n"
                    f"_(Sin descuento de stock)_"
                )
                await onboarding_service.upsert_sesion(negocio_id_str, "activo", {})
                await ycloud.send_text(from_number, conf)

            else:
                await ycloud.send_text(
                    from_number,
                    "No entendí 😅 Edita los datos, escribe *guardar* para confirmar, o *cancelar*."
                )
            return

        # ── FLUJO C: NLP activo (estado normal) ──


        # 1. Leer historial de corto plazo antes de llamar al modelo
        historial = onboarding_service._leer_historial(sesion)
        logger.info(f"[Historial] {from_number}: {len(historial)} mensajes previos")

        contexto = {
            "nombre":       negocio.get("nombre_negocio", ""),
            "tipo_ropa":    negocio.get("rubro", ""),
            "zona_horaria": negocio.get("zona_horaria", "America/Lima"),
        }
        result = await gemini_service.procesar_mensaje(
            mensaje=text,
            historial=historial,
            contexto_negocio=contexto,
        )

        intent    = result.get("intent", "DESCONOCIDO")
        datos     = result.get("datos", {})
        respuesta = result.get("respuesta", "¿En qué te ayudo? 😊")

        logger.info(f"[NLP] intent={intent} | datos={datos}")

        # Zona horaria local para fecha/hora de registros
        from zoneinfo import ZoneInfo
        zona_str  = negocio.get("zona_horaria") or "America/Lima"
        try:
            tz = ZoneInfo(zona_str)
        except Exception:
            tz = ZoneInfo("America/Lima")
        ahora_local = datetime.datetime.now(tz)
        f_fecha     = datos.get("fecha") or ahora_local.strftime("%Y-%m-%d")
        f_hora      = datos.get("hora")  or ahora_local.strftime("%H:%M:%S")

        simbolos = {"PEN": "S/", "USD": "$", "BOB": "Bs.", "CLP": "CLP"}
        simbolo  = simbolos.get(datos.get("moneda", "PEN"), "S/")

        # ── Intent INCOMPLETO — el modelo necesita más info antes de registrar ──
        if intent == "INCOMPLETO":
            await ycloud.send_text(from_number, respuesta)
            # Guardar en historial para que el siguiente mensaje tenga contexto
            try:
                await onboarding_service.guardar_en_historial(
                    negocio_id_str, sesion, text, respuesta
                )
            except Exception as e:
                logger.warning(f"[Historial] No se pudo guardar INCOMPLETO: {e}")
            if es_audio:
                await _responder_con_voz(from_number, respuesta)
            return


        # ── Intent VENTA ──
        if intent == "VENTA" and datos.get("total"):
            nombre_producto  = datos.get("producto", "producto")
            cantidad         = int(datos.get("cantidad", 1))
            precio_unitario  = datos.get("precio_unitario")
            nombre_propio    = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"
            total_venta      = datos.get("total", 0)
            moneda_venta     = datos.get("moneda", "PEN")

            # Datos de la venta para guardar en sesión si hay pasos intermedios
            datos_venta_pendiente = {
                "nombre_producto_original": nombre_producto,
                "cantidad_stock":           cantidad,
                "precio_unitario_stock":    precio_unitario,
                "venta_monto":              total_venta,
                "venta_moneda":             simbolo,
                "venta_fecha":              f_fecha,
                "venta_hora":               f_hora,
                "venta_nombre_propio":      nombre_propio,
                "venta_moneda_codigo":      moneda_venta,
            }

            # 1. Solo matching — no registramos aún
            resultado_stock = await stock_service.procesar_venta(
                negocio_id      = negocio_id_str,
                nombre_producto = nombre_producto,
                cantidad        = cantidad,
                precio_unitario = precio_unitario,
            )
            estado_stock = resultado_stock["estado"]

            # Helper: construir mensaje confirmación de venta
            def _msg_confirmacion_venta(nombre_final: str) -> str:
                return (
                    f"✅ Venta registrada, {nombre_propio}\n\n"
                    f"📅 {f_fecha} {f_hora}\n"
                    f"📝 Producto: {nombre_final}\n"
                    f"📦 Cantidad: {cantidad}\n"
                    f"💰 Total: {simbolo} {total_venta:.2f}"
                )

            # ── CASO A: match exacto → registrar + descontar + confirmar ──
            if estado_stock == "exacto":
                producto_id_match = resultado_stock["producto_id"]
                prod_nombre = resultado_stock["producto_nombre"]
                prod_talla  = resultado_stock["producto_talla"]
                nombre_final = prod_nombre + (f" Talla {prod_talla}" if prod_talla else "")

                # Registrar transacción con producto_id
                transaccion_id = await _guardar_transaccion_retornando_id(
                    negocio_id  = negocio_id_str,
                    tipo        = "venta",
                    descripcion = nombre_final,
                    monto       = total_venta,
                    moneda      = moneda_venta,
                    fecha       = datos.get("fecha"),
                    hora        = datos.get("hora"),
                    cantidad    = cantidad,
                    producto_id = producto_id_match,
                )

                # Descontar stock
                descuento = await stock_service.ejecutar_descuento_venta(
                    negocio_id      = negocio_id_str,
                    producto_id     = producto_id_match,
                    nombre_producto = nombre_producto,
                    cantidad        = cantidad,
                    transaccion_id  = transaccion_id,
                )

                respuesta_venta = _msg_confirmacion_venta(nombre_final)
                await ycloud.send_text(from_number, respuesta_venta)
                await ycloud.send_text(from_number, descuento["mensaje_stock"])
                try:
                    await onboarding_service.guardar_en_historial(negocio_id_str, sesion, text, respuesta_venta)
                except Exception:
                    pass

            # ── CASO B: candidatos múltiples → guardar en sesión, preguntar ──
            elif estado_stock == "parcial":
                candidatos = resultado_stock["candidatos"]
                lista = "\n".join(
                    f"{i+1}. {p['nombre']}" + (f", Talla {p['talla']}" if p.get("talla") else "")
                    + f" (stock: {p.get('cantidad_actual', '?')})"
                    for i, p in enumerate(candidatos)
                )
                msg_lista = (
                    f"¿Cuál de estos vendiste? 🤔\n\n"
                    f"{lista}\n\n"
                    f"Escribe el número, o *0* si no está en la lista."
                )
                datos_venta_pendiente["candidatos_stock"] = candidatos
                datos_venta_pendiente["operacion_stock"]  = "venta"
                await onboarding_service.upsert_sesion(
                    negocio_id_str, "ESPERANDO_SELECCION_STOCK", datos_venta_pendiente
                )
                await ycloud.send_text(from_number, msg_lista)

            # ── CASO C: sin match → preguntar si agregar o seguir sin stock ──
            elif estado_stock == "sin_match":
                msg_decision = (
                    f"Este producto no está en tu catálogo 🔍\n\n"
                    f"¿Qué quieres hacer?\n"
                    f"1️⃣ *Agregar* al catálogo\n"
                    f"2️⃣ *Seguir* sin añadir al stock\n\n"
                    f"_(La venta se registrará cuando elijas)_"
                )
                await onboarding_service.upsert_sesion(
                    negocio_id_str, "ESPERANDO_DECISION_PRODUCTO_NUEVO", datos_venta_pendiente
                )
                await ycloud.send_text(from_number, msg_decision)

            if es_audio:
                await _responder_con_voz(from_number, respuesta)
            return


        # ── Intent GASTO ──
        elif intent == "GASTO" and datos.get("monto"):
            nombre_propio = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"
            await _guardar_transaccion(
                negocio_id  = negocio_id_str,
                tipo        = "gasto",
                descripcion = datos.get("concepto", "gasto"),
                monto       = datos.get("monto", 0),
                moneda      = datos.get("moneda", "PEN"),
                fecha       = datos.get("fecha"),
                hora        = datos.get("hora"),
            )
            respuesta = (
                f'✅ Gasto registrado, {nombre_propio}\n\n'
                f"📅 {f_fecha} {f_hora}\n"
                f"🏷️ {str(datos.get('categoria', 'Otros')).capitalize()}\n"
                f"📝 {datos.get('concepto', '')}\n"
                f"💰 {simbolo} {datos.get('monto', 0):.2f}"
            )

        # ── Intent INVENTARIO ──
        elif intent == "INVENTARIO" and datos.get("producto"):
            nombre_producto = datos.get("producto", "")
            cantidad        = int(datos.get("cantidad", 0))
            tipo_inv        = datos.get("tipo", "entrada")
            precio_costo    = datos.get("precio_costo")
            precio_venta    = datos.get("precio_venta")

            resultado_inv = await stock_service.procesar_inventario(
                negocio_id      = negocio_id_str,
                nombre_producto = nombre_producto,
                cantidad        = cantidad,
                tipo            = tipo_inv,
                precio_costo    = precio_costo,
                precio_venta    = precio_venta,
            )

            estado_inv = resultado_inv["estado"]

            if estado_inv in ("actualizado", "creado"):
                respuesta = resultado_inv["mensaje"]

            elif estado_inv == "pendiente_seleccion":
                await onboarding_service.upsert_sesion(
                    negocio_id_str,
                    "ESPERANDO_SELECCION_STOCK",
                    {
                        "candidatos_stock":         resultado_inv["candidatos"],
                        "operacion_stock":          f"inventario_{tipo_inv}",
                        "cantidad_stock":           cantidad,
                        "nombre_producto_original": nombre_producto,
                    },
                )
                respuesta = resultado_inv["mensaje"]

        # ── Intent REPORTE ──
        elif intent == "REPORTE":
            datos_reporte = await _obtener_reporte(negocio_id_str, datos.get("periodo", "hoy"))
            respuesta     = await gemini_service.generar_resumen_reporte(datos_reporte)

        # ── Intents ELIMINAR / EDITAR ──
        elif intent in ["ELIMINAR_TRANSACCION", "EDITAR_TRANSACCION"]:
            ultimas = await _obtener_ultimas_transacciones(negocio_id_str, 5)
            if not ultimas:
                respuesta = "No tienes transacciones recientes para modificar."
            else:
                accion      = "eliminar" if intent == "ELIMINAR_TRANSACCION" else "editar"
                nuevo_estado = (
                    "ESPERANDO_SELECCION_ELIMINAR"
                    if intent == "ELIMINAR_TRANSACCION"
                    else "ESPERANDO_SELECCION_EDITAR"
                )
                await onboarding_service.upsert_sesion(
                    negocio_id_str, nuevo_estado, {"ultimas_transacciones": ultimas}
                )
                simbolos_moneda = {"PEN": "S/", "USD": "$", "BOB": "Bs.", "CLP": "CLP"}
                lineas = [f"¿Cuál deseas {accion}?"]
                for i, t in enumerate(ultimas, 1):
                    s = simbolos_moneda.get(t["moneda"], t["moneda"])
                    lineas.append(f"{i}. {t.get('fecha_corta','')} | {t['descripcion']} | {s} {t['monto']:.2f}")
                lineas.append("\nEscribe el número o 'cancelar'.")
                respuesta = "\n".join(lineas)

        # ── Enviar respuesta final + guardar historial ──
        await ycloud.send_text(from_number, respuesta)

        try:
            await onboarding_service.guardar_en_historial(
                negocio_id_str, sesion, text, respuesta
            )
        except Exception as e:
            logger.warning(f"[Historial] No se pudo guardar: {e}")

        if es_audio:
            await _responder_con_voz(from_number, respuesta)

    except Exception as e:
        logger.error(f"[_process_text] Error: {e}", exc_info=True)
        await ycloud.send_text(from_number, "Tuve un error técnico. Intenta de nuevo 🙏")


# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────

async def _responder_con_voz(from_number: str, texto: str) -> None:
    try:
        local_path = await polly_service.text_to_speech(texto)
        if not local_path:
            return
        file_name   = f"voz_{uuid.uuid4()}.mp3"
        url_publica = await upload_audio_to_supabase(local_path, file_name)
        await ycloud.send_audio(from_number, url_publica)
        if os.path.exists(local_path):
            os.remove(local_path)
    except Exception as e:
        logger.error(f"[Voz] Error al generar/enviar audio: {e}")


async def _guardar_transaccion(
    negocio_id: str,
    tipo: str,
    descripcion: str,
    monto: float,
    moneda: str = "PEN",
    fecha: str = None,
    hora: str = None,
    cantidad: int = 1,
) -> None:
    """Guarda transacción sin retornar el id. Para gastos."""
    await _guardar_transaccion_retornando_id(
        negocio_id, tipo, descripcion, monto, moneda, fecha, hora, cantidad
    )


async def _guardar_transaccion_retornando_id(
    negocio_id: str,
    tipo: str,
    descripcion: str,
    monto: float,
    moneda: str = "PEN",
    fecha: str = None,
    hora: str = None,
    cantidad: int = 1,
    producto_id: str | None = None,
) -> str | None:
    """
    Guarda la transacción y retorna su UUID como string.
    Necesario para enlazar stock_movimientos.transaccion_id.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if fecha and hora:
            try:
                fecha_obj = datetime.date.fromisoformat(fecha)
                hora_obj  = datetime.time.fromisoformat(hora)
            except ValueError:
                fecha_obj = datetime.date.today()
                hora_obj  = datetime.time(12, 0)

            row = await conn.fetchrow(
                """
                INSERT INTO transacciones
                    (negocio_id, tipo, descripcion, monto, moneda, fecha, hora, origen_registro, cantidad, producto_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'whatsapp', $8, $9)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto), moneda, fecha_obj, hora_obj, cantidad, producto_id,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO transacciones
                    (negocio_id, tipo, descripcion, monto, moneda, fecha, origen_registro, cantidad, producto_id)
                VALUES ($1, $2, $3, $4, $5, CURRENT_DATE, 'whatsapp', $6, $7)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto), moneda, cantidad, producto_id,
            )

    tx_id = row["id"] if row else None
    logger.info(f"💾 {tipo.upper()}: {descripcion} | {moneda} {monto} | id={tx_id} | producto_id={producto_id}")
    return tx_id



async def _obtener_reporte(negocio_id: str, periodo: str) -> dict:
    filtros = {
        "hoy":   "fecha = CURRENT_DATE",
        "ayer":  "fecha = CURRENT_DATE - 1",
        "semana":"fecha >= CURRENT_DATE - 7",
        "mes":   "fecha >= CURRENT_DATE - 30",
    }
    where = filtros.get(periodo, filtros["hoy"])
    pool  = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'gasto'), 0) AS total_gastos,
                COUNT(*)           FILTER (WHERE tipo = 'venta')       AS num_ventas
            FROM transacciones
            WHERE negocio_id = $1 AND {where}
            """,
            negocio_id,
        )
    tv = float(row["total_ventas"])
    tg = float(row["total_gastos"])
    return {
        "periodo":           periodo,
        "total_ventas":      tv,
        "total_gastos":      tg,
        "num_transacciones": int(row["num_ventas"]),
        "ganancia_neta":     tv - tg,
    }


async def _obtener_ultimas_transacciones(negocio_id: str, limite: int = 5) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.id,
                t.tipo,
                t.descripcion,
                t.monto,
                t.moneda,
                to_char(t.created_at AT TIME ZONE COALESCE(n.zona_horaria, 'America/Lima'), 'DD/MM') AS fecha_corta
            FROM transacciones t
            JOIN negocios n ON n.id = t.negocio_id
            WHERE t.negocio_id = $1::uuid
            ORDER BY t.created_at DESC
            LIMIT $2
            """,
            negocio_id, limite,
        )
    res = []
    for r in rows:
        d = dict(r)
        d["id"]    = str(d["id"])
        d["monto"] = float(d["monto"]) if d.get("monto") is not None else 0.0
        res.append(d)
    return res


async def _eliminar_transaccion(transaccion_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM transacciones WHERE id = $1::uuid", transaccion_id)
    logger.info(f"🗑️ Transacción eliminada: {transaccion_id}")


async def _actualizar_transaccion(transaccion_id: str, actualizaciones: dict) -> None:
    if not actualizaciones:
        return
    sets   = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(actualizaciones))
    valores = list(actualizaciones.values())
    pool   = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE transacciones SET {sets} WHERE id = $1::uuid",
            transaccion_id, *valores,
        )
    logger.info(f"✏️ Transacción actualizada: {transaccion_id} | {actualizaciones}")