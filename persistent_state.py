"""Estado persistente del agente en PostgreSQL/Neon.

El agente original usa dos diccionarios en RAM. Esta capa mantiene la misma
interfaz basica para no alterar la logica conversacional, pero persiste los
cambios en PostgreSQL para sobrevivir reinicios y multiples workers.
"""

import json
import os
from contextlib import contextmanager

import psycopg2


DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA_SQL = """
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
"""


def _require_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no esta configurada")


@contextmanager
def _connection():
    _require_db()
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema():
    """Crea las tablas si aun no existen; es idempotente y segura para deploys."""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)


def _wrap(value, save_callback):
    if isinstance(value, dict):
        return _PersistentDict(value, save_callback)
    if isinstance(value, list):
        return _PersistentList(value, save_callback)
    return value


class _PersistentDict(dict):
    def __init__(self, value, save_callback):
        super().__init__(value)
        self._save_callback = save_callback

    def _save(self):
        self._save_callback(self)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._save()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._save()

    def pop(self, *args):
        value = super().pop(*args)
        self._save()
        return value

    def clear(self):
        super().clear()
        self._save()

    def setdefault(self, key, default=None):
        if key in self:
            return self[key]
        super().__setitem__(key, default)
        self._save()
        return default


class _PersistentList(list):
    def __init__(self, value, save_callback):
        super().__init__(value)
        self._save_callback = save_callback

    def _save(self):
        self._save_callback(self)

    def append(self, value):
        super().append(value)
        self._save()

    def extend(self, values):
        super().extend(values)
        self._save()

    def insert(self, index, value):
        super().insert(index, value)
        self._save()

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        self._save()

    def __delitem__(self, index):
        super().__delitem__(index)
        self._save()

    def pop(self, *args):
        value = super().pop(*args)
        self._save()
        return value

    def remove(self, value):
        super().remove(value)
        self._save()

    def clear(self):
        super().clear()
        self._save()


class PersistentState:
    """Map-like state persistido por numero de WhatsApp."""

    def __init__(self, field):
        if field not in ("memoria_chat", "obligacion_activa"):
            raise ValueError("Campo de estado no permitido")
        self.field = field
        ensure_schema()

    def _load(self, numero):
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self.field} FROM bot_estado_persistente WHERE numero_telefono = %s",
                    (str(numero),),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            raise KeyError(numero)
        return row[0]

    def _save(self, numero, value):
        payload = json.dumps(value, ensure_ascii=False)
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO bot_estado_persistente (numero_telefono, {self.field})
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (numero_telefono) DO UPDATE SET
                        {self.field} = EXCLUDED.{self.field},
                        actualizado_en = NOW()
                    """,
                    (str(numero), payload),
                )

    def __getitem__(self, numero):
        numero = str(numero)
        value = self._load(numero)
        return _wrap(value, lambda new_value: self._save(numero, new_value))

    def __setitem__(self, numero, value):
        self._save(str(numero), value)

    def __contains__(self, numero):
        try:
            self._load(str(numero))
            return True
        except KeyError:
            return False

    def get(self, numero, default=None):
        try:
            return self[numero]
        except KeyError:
            return default

    def setdefault(self, numero, default=None):
        try:
            return self[numero]
        except KeyError:
            self[numero] = default
            return self[numero]

    def __delitem__(self, numero):
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM bot_estado_persistente WHERE numero_telefono = %s",
                    (str(numero),),
                )


def registrar_evento(message_id, numero_telefono):
    """Registra un mensaje y devuelve False si ya habia sido procesado."""
    if not message_id:
        return True
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_eventos_procesados (message_id, numero_telefono)
                VALUES (%s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """,
                (str(message_id), str(numero_telefono) if numero_telefono else None),
            )
            return cur.fetchone() is not None
