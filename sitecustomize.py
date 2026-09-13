"""Runtime compatibility patch for the WhatsApp debt agent.

The deployed agent contains a PostgreSQL query that combines SELECT DISTINCT
with ORDER BY CASE expressions that are not in the select list. PostgreSQL
rejects that query. We surgically remove DISTINCT only for that codeudor query;
the application already performs its own deterministic selection afterwards.
"""

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
                # The result columns must remain exactly the six columns expected
                # by agente_cobranzas.py, so do not add ORDER BY expressions to SELECT.
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
