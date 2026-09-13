-- Persistencia de estado del agente y deduplicacion de mensajes de WhatsApp.
-- Puede ejecutarse varias veces sin romper datos existentes.

CREATE TABLE IF NOT EXISTS bot_estado_persistente (
    numero_telefono TEXT PRIMARY KEY,
    memoria_chat JSONB NOT NULL DEFAULT '[]'::jsonb,
    obligacion_activa JSONB,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_eventos_procesados (
    message_id TEXT PRIMARY KEY,
    numero_telefono TEXT,
    recibido_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_eventos_procesados_numero
    ON bot_eventos_procesados (numero_telefono);

CREATE INDEX IF NOT EXISTS idx_bot_estado_persistente_actualizado
    ON bot_estado_persistente (actualizado_en);
