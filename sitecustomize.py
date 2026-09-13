"""Runtime compatibility and production hardening hooks for the debt agent.

1. Fixes the PostgreSQL DISTINCT/ORDER BY incompatibility from the legacy
   codeudor query.
2. Replaces the agent's in-memory conversation/obligation dictionaries with
   PostgreSQL-backed state after agente_cobranzas is imported.
3. Deduplicates Meta/WhatsApp message IDs before the message is processed.
"""

import builtins

try:
    import psycopg2
except Exception:
    psycopg2 = None


if psycopg2 is not None:
    _original_connect = psycopg2.connect

    class _CursorProxy:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, query, vars=None):
            if (
                isinstance(query, str)
                and "procesos_litisconsorcio" in query
                and "SELECT DISTINCT" in query
                and "ORDER BY" in query
                and "LOWER(COALESCE(p.estado" in query
            ):
                query = query.replace("SELECT DISTINCT", "SELECT", 1)
            return self._cursor.execute(query, vars)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

        def __enter__(self):
            self._cursor.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._cursor.__exit__(exc_type, exc_value, traceback)

    class _ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self, *args, **kwargs):
            return _CursorProxy(self._connection.cursor(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._connection.__exit__(exc_type, exc_value, traceback)

    def _patched_connect(*args, **kwargs):
        return _ConnectionProxy(_original_connect(*args, **kwargs))

    psycopg2.connect = _patched_connect


_state_hook_installed = False
_original_import = builtins.__import__


def _persist_agent_state(module):
    global _state_hook_installed
    if getattr(module, "_PERSISTENT_STATE_INSTALLED", False):
        return

    try:
        from persistent_state import PersistentState, registrar_evento

        module.memoria_chats = PersistentState("memoria_chat")
        module.obligaciones_activas = PersistentState("obligacion_activa")

        original_processor = module.procesar_y_responder

        def procesar_y_responder_persistente(data):
            try:
                value = data["entry"][0]["changes"][0]["value"]
                message = (value.get("messages") or [{}])[0]
                message_id = message.get("id")
                numero = message.get("from")
                if not message_id:
                    return
                if not registrar_evento(message_id, numero):
                    print(f"ℹ️ Mensaje duplicado ignorado: {message_id}", flush=True)
                    return
            except Exception as exc:
                # Fail closed: si no podemos registrar idempotencia no ejecutamos
                # el proceso, evitando respuestas duplicadas en produccion.
                print(f"❌ No se pudo validar idempotencia: {repr(exc)}", flush=True)
                return
            return original_processor(data)

        module.procesar_y_responder = procesar_y_responder_persistente
        module._PERSISTENT_STATE_INSTALLED = True
        print("✅ Estado persistente PostgreSQL habilitado", flush=True)
    except Exception as exc:
        # El agente no debe arrancar con estado RAM-only si la capa persistente
        # no pudo inicializarse. El error queda visible en los logs de Render.
        print(f"❌ No se pudo habilitar estado persistente: {repr(exc)}", flush=True)
        raise


def _import_with_state_hook(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    target = name.split(".")[0]
    if target == "agente_cobranzas" and hasattr(module, "procesar_y_responder"):
        _persist_agent_state(module)
    return module


if not _state_hook_installed:
    builtins.__import__ = _import_with_state_hook
    _state_hook_installed = True
