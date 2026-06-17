-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.negocios (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  whatsapp_numero character varying UNIQUE,
  nombre_negocio character varying,
  nombre_propietario character varying,
  rubro character varying,
  ciudad character varying DEFAULT 'Tacna'::character varying,
  estado character varying DEFAULT 'activo'::character varying,
  onboarding_completo boolean DEFAULT false,
  idioma_preferido character varying DEFAULT 'es'::character varying,
  zona_horaria character varying DEFAULT 'America/Lima'::character varying,
  atiende_turistas_chilenos boolean DEFAULT false,
  atiende_clientes_bolivianos boolean DEFAULT false,
  proveedores_zona_franca boolean DEFAULT false,
  proveedores_bolivia boolean DEFAULT false,
  monedas_aceptadas character varying DEFAULT 'PEN'::character varying,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  horario_cierre character varying DEFAULT '20:00'::character varying,
  numero_principal_id uuid,
  CONSTRAINT negocios_pkey PRIMARY KEY (id),
  CONSTRAINT negocios_numero_principal_id_fkey FOREIGN KEY (numero_principal_id) REFERENCES public.usuarios(id)
);
CREATE TABLE public.sesiones (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL UNIQUE,
  estado_conversacion character varying DEFAULT 'inicio'::character varying,
  datos_temporales jsonb DEFAULT '{}'::jsonb,
  ultimo_mensaje_at timestamp with time zone DEFAULT now(),
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT sesiones_pkey PRIMARY KEY (id),
  CONSTRAINT sesiones_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.tipos_cambio (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  fecha date NOT NULL DEFAULT CURRENT_DATE,
  moneda_origen character varying NOT NULL,
  moneda_destino character varying NOT NULL DEFAULT 'PEN'::character varying,
  tasa numeric NOT NULL,
  fuente character varying DEFAULT 'manual'::character varying,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT tipos_cambio_pkey PRIMARY KEY (id)
);
CREATE TABLE public.categorias (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  nombre character varying NOT NULL,
  tipo character varying NOT NULL,
  color character varying,
  activa boolean DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT categorias_pkey PRIMARY KEY (id),
  CONSTRAINT categorias_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.transacciones (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  tipo character varying NOT NULL,
  monto numeric NOT NULL,
  moneda character varying DEFAULT 'PEN'::character varying,
  tasa_cambio_usada numeric,
  monto_pen numeric,
  categoria_id uuid,
  descripcion text,
  origen_cliente character varying DEFAULT 'local'::character varying,
  metodo_pago character varying,
  fecha date NOT NULL DEFAULT CURRENT_DATE,
  hora time without time zone DEFAULT CURRENT_TIME,
  notas text,
  origen_registro character varying DEFAULT 'whatsapp'::character varying,
  created_at timestamp with time zone DEFAULT now(),
  cantidad integer DEFAULT 1,
  producto_id uuid,
  CONSTRAINT transacciones_pkey PRIMARY KEY (id),
  CONSTRAINT transacciones_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id),
  CONSTRAINT transacciones_categoria_id_fkey FOREIGN KEY (categoria_id) REFERENCES public.categorias(id),
  CONSTRAINT transacciones_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos(id)
);
CREATE TABLE public.inventario (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  producto character varying NOT NULL,
  categoria character varying,
  cantidad_actual integer DEFAULT 0,
  cantidad_minima integer DEFAULT 5,
  precio_costo numeric,
  precio_costo_moneda character varying DEFAULT 'PEN'::character varying,
  precio_venta_pen numeric,
  unidad character varying DEFAULT 'unidad'::character varying,
  activo boolean DEFAULT true,
  ultima_actualizacion timestamp with time zone DEFAULT now(),
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT inventario_pkey PRIMARY KEY (id),
  CONSTRAINT inventario_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.recordatorios (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  tipo character varying NOT NULL,
  mensaje text NOT NULL,
  frecuencia character varying,
  hora_envio time without time zone DEFAULT '08:00:00'::time without time zone,
  dia_semana integer,
  dia_mes integer,
  fecha_especifica date,
  activo boolean DEFAULT true,
  ultimo_envio_at timestamp with time zone,
  created_at timestamp with time zone DEFAULT now(),
  enviado boolean DEFAULT false,
  fecha_hora timestamp with time zone,
  estado character varying DEFAULT 'pendiente'::character varying,
  enviado_at timestamp with time zone,
  CONSTRAINT recordatorios_pkey PRIMARY KEY (id),
  CONSTRAINT recordatorios_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.metricas (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  fecha date NOT NULL DEFAULT CURRENT_DATE,
  mensajes_recibidos integer DEFAULT 0,
  mensajes_enviados integer DEFAULT 0,
  ventas_registradas integer DEFAULT 0,
  gastos_registrados integer DEFAULT 0,
  consultas_inventario integer DEFAULT 0,
  reportes_generados integer DEFAULT 0,
  audios_transcritos integer DEFAULT 0,
  ventas_a_chilenos integer DEFAULT 0,
  ventas_a_bolivianos integer DEFAULT 0,
  ventas_en_clp numeric DEFAULT 0,
  ventas_en_bob numeric DEFAULT 0,
  ventas_en_usd numeric DEFAULT 0,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT metricas_pkey PRIMARY KEY (id),
  CONSTRAINT metricas_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.categorias_plantilla (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  tipo_ropa character varying NOT NULL UNIQUE,
  categorias jsonb NOT NULL DEFAULT '[]'::jsonb,
  generado_por_ia boolean DEFAULT true,
  veces_usado integer DEFAULT 1,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT categorias_plantilla_pkey PRIMARY KEY (id)
);
CREATE TABLE public.productos (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL,
  categoria_id uuid,
  nombre character varying NOT NULL,
  nombre_variantes ARRAY,
  precio_costo numeric,
  precio_costo_moneda character varying DEFAULT 'PEN'::character varying,
  precio_venta_pen numeric,
  unidad character varying DEFAULT 'unidad'::character varying,
  activo boolean DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  talla text,
  CONSTRAINT productos_pkey PRIMARY KEY (id),
  CONSTRAINT productos_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id),
  CONSTRAINT productos_categoria_id_fkey FOREIGN KEY (categoria_id) REFERENCES public.categorias(id)
);
CREATE TABLE public.stock (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  producto_id uuid NOT NULL UNIQUE,
  cantidad_actual integer NOT NULL DEFAULT 0,
  cantidad_minima integer NOT NULL DEFAULT 5,
  ultima_actualizacion timestamp with time zone DEFAULT now(),
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT stock_pkey PRIMARY KEY (id),
  CONSTRAINT stock_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos(id)
);
CREATE TABLE public.stock_movimientos (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  producto_id uuid NOT NULL,
  negocio_id uuid NOT NULL,
  tipo character varying NOT NULL CHECK (tipo::text = ANY (ARRAY['entrada'::character varying, 'salida'::character varying]::text[])),
  cantidad integer NOT NULL CHECK (cantidad > 0),
  cantidad_antes integer NOT NULL,
  cantidad_despues integer NOT NULL,
  motivo character varying,
  transaccion_id uuid,
  notas text,
  fecha date NOT NULL DEFAULT CURRENT_DATE,
  hora time without time zone NOT NULL DEFAULT CURRENT_TIME,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT stock_movimientos_pkey PRIMARY KEY (id),
  CONSTRAINT stock_movimientos_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos(id),
  CONSTRAINT stock_movimientos_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id),
  CONSTRAINT stock_movimientos_transaccion_id_fkey FOREIGN KEY (transaccion_id) REFERENCES public.transacciones(id)
);
CREATE TABLE public.usuarios (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  numero character varying NOT NULL UNIQUE,
  negocio_id uuid,
  es_principal boolean DEFAULT false,
  verificado boolean DEFAULT false,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  nombre character varying,
  CONSTRAINT usuarios_pkey PRIMARY KEY (id),
  CONSTRAINT numeros_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id)
);
CREATE TABLE public.suscripciones_negocio (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  negocio_id uuid NOT NULL UNIQUE,
  estado character varying NOT NULL DEFAULT 'activa'::character varying,
  fecha_inicio date NOT NULL DEFAULT CURRENT_DATE,
  fecha_renovacion date,
  fecha_cancelacion date,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  plan_id uuid NOT NULL,
  metodo_pago character varying,
  CONSTRAINT suscripciones_negocio_pkey PRIMARY KEY (id),
  CONSTRAINT suscripciones_negocio_id_fkey FOREIGN KEY (negocio_id) REFERENCES public.negocios(id),
  CONSTRAINT suscripciones_negocio_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.planes_suscripcion(id)
);
CREATE TABLE public.planes_suscripcion (
  id uuid NOT NULL DEFAULT uuid_generate_v4(),
  codigo character varying NOT NULL UNIQUE,
  nombre character varying NOT NULL,
  descripcion text,
  precio_mensual numeric DEFAULT 0,
  precio_anual numeric DEFAULT 0,
  limite_numeros integer DEFAULT 1,
  limite_productos integer DEFAULT 100,
  limite_transacciones_mes integer DEFAULT 500,
  limite_usuarios integer DEFAULT 1,
  features jsonb DEFAULT '{}'::jsonb,
  activo boolean DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT planes_suscripcion_pkey PRIMARY KEY (id)
);