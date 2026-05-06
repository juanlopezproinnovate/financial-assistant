-- ============================================================
-- SCHEMA INICIAL — Chatbot WhatsApp para Comerciantes Tacna
-- Ejecutar en Supabase → SQL Editor
-- ============================================================

-- 1. NEGOCIOS — cada comerciante registrado en el bot
CREATE TABLE IF NOT EXISTS negocios (
    id          SERIAL PRIMARY KEY,
    whatsapp    VARCHAR(20)  NOT NULL UNIQUE,   -- número en formato E.164, ej: 51912345678
    nombre      VARCHAR(120) NOT NULL,
    rubro       VARCHAR(80),                    -- ropa, calzado, accesorios, etc.
    activo      BOOLEAN      NOT NULL DEFAULT TRUE,
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 2. CATEGORÍAS — categorías de productos del negocio
CREATE TABLE IF NOT EXISTS categorias (
    id          SERIAL PRIMARY KEY,
    negocio_id  INT          NOT NULL REFERENCES negocios(id) ON DELETE CASCADE,
    nombre      VARCHAR(80)  NOT NULL,
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 3. TRANSACCIONES — ventas e ingresos/egresos registrados por voz o texto
CREATE TABLE IF NOT EXISTS transacciones (
    id           SERIAL PRIMARY KEY,
    negocio_id   INT             NOT NULL REFERENCES negocios(id) ON DELETE CASCADE,
    categoria_id INT             REFERENCES categorias(id) ON DELETE SET NULL,
    tipo         VARCHAR(10)     NOT NULL CHECK (tipo IN ('ingreso', 'egreso')),
    monto        NUMERIC(10, 2)  NOT NULL,
    descripcion  TEXT,
    fecha        DATE            NOT NULL DEFAULT CURRENT_DATE,
    creado_en    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- 4. RECORDATORIOS — alertas programadas (pagos, pedidos, etc.)
CREATE TABLE IF NOT EXISTS recordatorios (
    id          SERIAL PRIMARY KEY,
    negocio_id  INT          NOT NULL REFERENCES negocios(id) ON DELETE CASCADE,
    mensaje     TEXT         NOT NULL,
    fecha_hora  TIMESTAMPTZ  NOT NULL,
    enviado     BOOLEAN      NOT NULL DEFAULT FALSE,
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 5. SESIONES — estado conversacional de cada usuario
CREATE TABLE IF NOT EXISTS sesiones (
    id          SERIAL PRIMARY KEY,
    negocio_id  INT          NOT NULL REFERENCES negocios(id) ON DELETE CASCADE,
    estado      VARCHAR(40)  NOT NULL DEFAULT 'inicio',  -- inicio, registrando, menu, etc.
    contexto    JSONB        NOT NULL DEFAULT '{}',      -- datos temporales del flujo
    actualizado TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 6. MÉTRICAS — eventos para análisis del MVP
CREATE TABLE IF NOT EXISTS metricas (
    id          SERIAL PRIMARY KEY,
    negocio_id  INT          REFERENCES negocios(id) ON DELETE SET NULL,
    evento      VARCHAR(60)  NOT NULL,   -- mensaje_recibido, voz_procesada, transaccion_creada, etc.
    detalle     JSONB        NOT NULL DEFAULT '{}',
    creado_en   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Índices útiles para consultas frecuentes
CREATE INDEX IF NOT EXISTS idx_transacciones_negocio_fecha ON transacciones(negocio_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_recordatorios_pendientes    ON recordatorios(fecha_hora) WHERE enviado = FALSE;
CREATE INDEX IF NOT EXISTS idx_metricas_evento             ON metricas(evento, creado_en DESC);
