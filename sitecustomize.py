"""Runtime compatibility patch for the WhatsApp debt agent.

PostgreSQL rejects SELECT DISTINCT queries whose ORDER BY expressions are not
present in the select list. The current deployed agent contains exactly that
pattern in the codeudor lookup. This small compatibility layer rewrites only
that query at runtime, without changing any financial logic.
"""

import re

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
            if isinstance(query, str) and "procesos_litisconsorcio" in query and "SELECT DISTINCT" in query:
                query = re.sub(
                    r"(\s+p\.estado\s*\n\s+FROM\s+procesos_litisconsorcio)",
                    "\\1",
                    query,
                    count=1,
                )

                # Add the ORDER BY expressions to SELECT DISTINCT as aliases.
                if "AS prioridad_estado" not in query:
                    query = query.replace(
                        "                        p.estado\n                    FROM procesos_litisconsorcio pl",
                        "                        p.estado,\n                        CASE WHEN LOWER(COALESCE(p.estado, '')) = 'activo' THEN 0 ELSE 1 END AS prioridad_estado,\n                        CASE WHEN COALESCE(pl.es_principal, false) THEN 0 ELSE 1 END AS prioridad_principal\n                    FROM procesos_litisconsorcio pl",
                        1,
                    )
                    query = query.replace(
                        "                        CASE WHEN LOWER(COALESCE(p.estado, '')) = 'activo' THEN 0 ELSE 1 END,\n                        CASE WHEN COALESCE(pl.es_principal, false) THEN 0 ELSE 1 END,\n                        p.inmueble_id",
                        "                        prioridad_estado,\n                        prioridad_principal,\n                        p.inmueble_id",
                        1,
                    )
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
