"""La identidad del demandado sale de proceso_partes + contactos, sin la tabla eliminada."""

import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import agente_cobranzas
import property_identity


ROOT = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, fetchall=None, fetchone=None):
        self.queries = []
        self._fetchall = fetchall or (lambda query: [])
        self._fetchone = fetchone or (lambda query: (False,))

    def execute(self, query, vars=None):
        self.queries.append((query, vars))

    def fetchall(self):
        return self._fetchall(self.queries[-1])

    def fetchone(self):
        return self._fetchone(self.queries[-1])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CanonicalDemandadoSchemaTests(unittest.TestCase):
    def test_el_repo_no_consulta_la_tabla_eliminada(self):
        for path in ROOT.rglob("*.py"):
            if ".git" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("procesos_litisconsorcio", text, path.name)
            self.assertNotIn("identificacion_demandado", text, path.name)

    def test_sitecustomize_no_reescribe_sql_legacy(self):
        text = (ROOT / "sitecustomize.py").read_text(encoding="utf-8")
        self.assertNotIn("_CursorProxy", text)
        self.assertNotIn("procesos_litisconsorcio", text)

    def test_buscar_deuda_mapea_demandado_desde_proceso_partes(self):
        def fetchall(query):
            sql = query[0]
            if "FROM inmuebles_ph" in sql:
                return []
            return [
                (77, "Ana Ruiz", "42.151.328", "RAD-9", True, "Activo", date(2024, 3, 2)),
                (77, "Ana Ruiz", "42151328", "RAD-1", False, "Activo", date(2023, 1, 1)),
            ]

        cursor = _Cursor(fetchall=fetchall)
        agente_cobranzas.obligaciones_activas.pop("573001112233", None)
        with patch.object(agente_cobranzas, "DATABASE_URL", "postgres://test"), patch.object(
            agente_cobranzas.psycopg2, "connect", return_value=_Conn(cursor)
        ):
            texto = agente_cobranzas.buscar_deuda_en_neon("42.151.328", "573001112233")

        self.assertIn("IDENTIDAD CONFIRMADA", texto)
        self.assertIn("RAD-9", texto)
        self.assertEqual(len(cursor.queries), 2)
        sql, params = cursor.queries[1]
        self.assertIn("FROM proceso_partes pp", sql)
        self.assertIn("INNER JOIN contactos c ON c.id = pp.contacto_id", sql)
        self.assertIn("pp.rol = 'DEMANDADO'", sql)
        self.assertIn("c.identificacion", sql)
        self.assertIn("pp.radicado_interno", sql)
        self.assertIn("pp.es_principal", sql)
        self.assertIn("pp.fecha_vinculacion", sql)
        self.assertNotIn("SELECT DISTINCT", sql)
        self.assertEqual(params, ("42151328",))

        estado = agente_cobranzas.obligaciones_activas["573001112233"]
        self.assertEqual(estado["tipo_relacion"], "CODEUDOR")
        self.assertEqual(estado["inmueble_id"], 77)
        self.assertEqual(estado["identificacion"], "42.151.328")
        self.assertEqual(estado["radicado_interno"], "RAD-9")
        self.assertIs(estado["es_principal"], True)
        self.assertEqual(estado["fecha_vinculacion"], "2024-03-02")
        json.dumps(estado)

    def test_buscar_deuda_conserva_al_titular_sin_proceso(self):
        def fetchall(query):
            if "FROM inmuebles_ph" in query[0]:
                return [(15, "Luis Mora", "1099887766")]
            return []

        cursor = _Cursor(fetchall=fetchall)
        with patch.object(agente_cobranzas, "DATABASE_URL", "postgres://test"), patch.object(
            agente_cobranzas.psycopg2, "connect", return_value=_Conn(cursor)
        ):
            texto = agente_cobranzas.buscar_deuda_en_neon("1099887766", "573009998877")

        self.assertIn("TITULAR", texto)
        estado = agente_cobranzas.obligaciones_activas["573009998877"]
        self.assertEqual(estado["tipo_relacion"], "TITULAR")
        self.assertEqual(estado["inmueble_id"], 15)
        self.assertIsNone(estado["radicado_interno"])
        self.assertIsNone(estado["es_principal"])
        self.assertIsNone(estado["fecha_vinculacion"])
        self.assertIn("pp.rol = 'DEMANDADO'", cursor.queries[1][0])

    def test_find_property_matches_acepta_demandado_por_contacto(self):
        cursor = _Cursor(fetchall=lambda query: [(5, "Conjunto Norte", "25-203", "42151328", "Ana")])
        with patch.object(property_identity, "_connection", return_value=_Conn(cursor)):
            matches = property_identity.find_property_matches("25", "203", cedula="42.151.328")

        self.assertEqual(matches[0]["inmueble_id"], 5)
        sql, params = cursor.queries[0]
        self.assertIn("JOIN proceso_partes pp ON pp.radicado_interno = p.radicado_interno", sql)
        self.assertIn("JOIN contactos cd ON cd.id = pp.contacto_id", sql)
        self.assertIn("pp.rol = 'DEMANDADO'", sql)
        self.assertIn("cd.identificacion", sql)
        self.assertEqual(params[-2:], ["42151328", "42151328"])

    def test_verify_identity_consulta_demandado_canonico(self):
        cursor = _Cursor(fetchone=lambda query: (True,))
        with patch.object(property_identity, "_connection", return_value=_Conn(cursor)):
            self.assertTrue(property_identity.verify_identity_for_property("42151328", 5))

        sql, params = cursor.queries[0]
        self.assertIn("JOIN proceso_partes pp ON pp.radicado_interno = p.radicado_interno", sql)
        self.assertIn("JOIN contactos cd ON cd.id = pp.contacto_id", sql)
        self.assertIn("pp.rol = 'DEMANDADO'", sql)
        self.assertEqual(params, (5, "42151328", 5, "42151328"))
        self.assertFalse(property_identity.verify_identity_for_property("123", 5))


if __name__ == "__main__":
    unittest.main()
