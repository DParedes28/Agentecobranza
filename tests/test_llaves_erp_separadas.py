"""Llaves hacia el ERP separadas por propósito + rotación de la llave de supervisión."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("AGENT_SUPERVISION_KEY", "test-supervision-key")
os.environ.setdefault("BOT_MEDIA_PUBLIC_SECRET", "test-media-secret")
os.environ.setdefault("BOT_MEDIA_DIR", tempfile.mkdtemp(prefix="bot_media_test_"))

import control_humano as ch  # noqa: E402


class SupervisionKeyRotationTests(unittest.TestCase):
    def test_acepta_actual_y_siguiente(self):
        with patch.object(ch, "SUPERVISION_KEY", "actual-key"), patch.object(
            ch, "SUPERVISION_KEY_NEXT", "siguiente-key"
        ):
            self.assertTrue(ch._llave_supervision_valida("actual-key"))
            self.assertTrue(ch._llave_supervision_valida("siguiente-key"))
            self.assertFalse(ch._llave_supervision_valida("otra"))
            self.assertFalse(ch._llave_supervision_valida(""))
            self.assertFalse(ch._llave_supervision_valida(None))

    def test_sin_siguiente_solo_actual(self):
        with patch.object(ch, "SUPERVISION_KEY", "actual-key"), patch.object(
            ch, "SUPERVISION_KEY_NEXT", ""
        ):
            self.assertTrue(ch._llave_supervision_valida("actual-key"))
            self.assertFalse(ch._llave_supervision_valida("siguiente-key"))

    def test_sin_llaves_falla_cerrado(self):
        with patch.object(ch, "SUPERVISION_KEY", None), patch.object(
            ch, "SUPERVISION_KEY_NEXT", ""
        ):
            self.assertFalse(ch._llave_supervision_valida(""))
            self.assertFalse(ch._llave_supervision_valida("x"))


class LlavesErpTests(unittest.TestCase):
    def setUp(self):
        import agente_cobranzas as ag

        self.ag = ag

    def test_solo_heredada(self):
        with patch.object(self.ag, "LIQUIDADOR_API_KEY", "vieja"), patch.object(
            self.ag, "ERP_BOT_API_KEY", None
        ), patch.object(self.ag, "ERP_RECAUDOS_API_KEY", None):
            self.assertEqual(self.ag.llave_erp_bot(), "vieja")
            self.assertEqual(self.ag.llave_erp_recaudos(), "vieja")

    def test_nuevas_tienen_prioridad(self):
        with patch.object(self.ag, "LIQUIDADOR_API_KEY", "vieja"), patch.object(
            self.ag, "ERP_BOT_API_KEY", "bot-nueva"
        ), patch.object(self.ag, "ERP_RECAUDOS_API_KEY", "rec-nueva"):
            self.assertEqual(self.ag.llave_erp_bot(), "bot-nueva")
            self.assertEqual(self.ag.llave_erp_recaudos(), "rec-nueva")

    def test_bot_nueva_sin_recaudos_cae_a_heredada(self):
        with patch.object(self.ag, "LIQUIDADOR_API_KEY", "vieja"), patch.object(
            self.ag, "ERP_BOT_API_KEY", "bot-nueva"
        ), patch.object(self.ag, "ERP_RECAUDOS_API_KEY", None):
            self.assertEqual(self.ag.llave_erp_bot(), "bot-nueva")
            self.assertEqual(self.ag.llave_erp_recaudos(), "vieja")


if __name__ == "__main__":
    unittest.main()
