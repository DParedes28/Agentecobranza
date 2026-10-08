"""Handoff ESCALAR_HUMANO y límite de audios (sin eco infinito)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import agente_cobranzas as bot


class HandoffAudioTests(unittest.TestCase):
    def setUp(self):
        bot.obligaciones_activas.clear()
        bot.memoria_chats.clear()

    def test_primer_audio_pide_texto_sin_escalar(self):
        enviados = []

        def fake_send(numero, texto, mid=None):
            enviados.append(texto)
            return True

        with patch.object(bot, "enviar_mensaje_whatsapp", side_effect=fake_send), patch.object(
            bot, "escalar_a_humano", return_value=True
        ) as esc:
            ok = bot._manejar_audio_o_no_soportado("573001112233", "audio", "wamid.1")
            self.assertTrue(ok)
            self.assertEqual(bot.obligaciones_activas["573001112233"]["audio_count"], 1)
            self.assertIn("texto", enviados[0].lower())
            esc.assert_not_called()

    def test_segundo_audio_escala(self):
        bot.obligaciones_activas["573001112233"] = {"audio_count": 1}
        with patch.object(bot, "enviar_mensaje_whatsapp", return_value=True), patch.object(
            bot, "escalar_a_humano", return_value=True
        ) as esc:
            ok = bot._manejar_audio_o_no_soportado("573001112233", "audio", "wamid.2")
            self.assertTrue(ok)
            esc.assert_called_once()
            self.assertEqual(esc.call_args.kwargs.get("motivo") or esc.call_args[1].get("motivo"), "AUDIO_REPETIDO")

    def test_escalar_a_humano_delega_a_control(self):
        fake_ch = MagicMock()
        fake_ch.escalar_humano.return_value = True
        with patch.dict("sys.modules", {"control_humano": fake_ch}):
            # Re-import path uses import inside function
            ok = bot.escalar_a_humano("573001112233", motivo="ESCALAR_HUMANO")
            self.assertTrue(ok)
            fake_ch.escalar_humano.assert_called_once_with("573001112233", motivo="ESCALAR_HUMANO")


if __name__ == "__main__":
    unittest.main()
