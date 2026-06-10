"""
app/graph/state.py

Estado compartido que fluye entre todos los nodos del grafo.
Se persiste en sesiones.datos_temporales (jsonb) entre mensajes.
"""

from typing import TypedDict, Literal, Any


class QuriState(TypedDict, total=False):
    # ── Identificación ──────────────────────────────────────
    telefono: str
    negocio_id: str
    usage: dict[str, int]

    # ── Mensaje entrante ────────────────────────────────────
    mensaje: str
    es_audio: bool

    # ── Intent detectado por el router ──────────────────────
    intent: Literal[
        "VENTA", "GASTO", "INVENTARIO", "REPORTE",
        "ELIMINAR_TRANSACCION", "EDITAR_TRANSACCION",
        "INCOMPLETO", "SALUDO", "AYUDA", "DESCONOCIDO",
        "ONBOARDING",
    ]

    # ── Datos extraídos por el NLP ───────────────────────────
    datos_nlp: dict[str, Any]

    # ── Contexto del negocio (de la BD) ─────────────────────
    negocio: dict[str, Any]          # fila completa de negocios
    sesion: dict[str, Any]           # fila completa de sesiones

    # ── Historial de corto plazo para el LLM ────────────────
    historial: list[dict]            # [{role, content}, ...]

    # ── Respuesta final a enviar por WhatsApp ────────────────
    respuesta: str

    # ── Sub-estado para flujos que necesitan varios turnos ───
    # (venta esperando selección de producto, edición, etc.)
    sub_estado: str                  # ej: "ESPERANDO_SELECCION_STOCK"
    datos_pendientes: dict[str, Any] # datos guardados entre turnos

    # ── Control de flujo ────────────────────────────────────
    siguiente_nodo: str              # usado por el router para decidir
    finalizado: bool                 # True cuando hay respuesta lista
    items: list[dict]