"""Tests de supervisión: preview, wa_id vs PSID y metadata de media."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Secretos dummy para firmar URLs en tests.
os.environ.setdefault("AGENT_SUPERVISION_KEY", "test-supervision-key")
os.environ.setdefault("BOT_MEDIA_PUBLIC_SECRET", "test-media-secret")
os.environ.setdefault("BOT_MEDIA_DIR", tempfile.mkdtemp(prefix="bot_media_test_"))

import control_humano as ch  # noqa: E402


class TelefonoLegibleTests(unittest.TestCase):
    def test_celular_co(self):
        self.assertTrue(ch.es_telefono_whatsapp_legible("573126487636"))
        self.assertTrue(ch.es_telefono_whatsapp_legible("3126487636"))

    def test_psid_opaco(self):
        self.assertFalse(ch.es_telefono_whatsapp_legible("1442782103907655"))
        self.assertFalse(ch.es_telefono_whatsapp_legible(""))


class ResolverIdentidadTests(unittest.TestCase):
    def test_prefiere_wa_id_cuando_es_telefono(self):
        message = {"from": "1442782103907655"}
        value = {"contacts": [{"wa_id": "573126487636"}]}
        clave, meta = ch.resolver_identidad_whatsapp(message, value)
        self.assertEqual(clave, "573126487636")
        self.assertEqual(meta.get("wa_id"), "573126487636")
        self.assertEqual(meta.get("psid"), "1442782103907655")

    def test_usa_from_si_es_telefono(self):
        message = {"from": "573001112233"}
        value = {"contacts": [{"wa_id": "573001112233"}]}
        clave, meta = ch.resolver_identidad_whatsapp(message, value)
        self.assertEqual(clave, "573001112233")
        self.assertEqual(meta.get("wa_id"), "573001112233")

    def test_fallback_psid(self):
        message = {"from": "1442782103907655"}
        value = {"contacts": [{"wa_id": "1442782103907655"}]}
        clave, meta = ch.resolver_identidad_whatsapp(message, value)
        self.assertEqual(clave, "1442782103907655")
        self.assertEqual(meta.get("psid"), "1442782103907655")


class PreviewTests(unittest.TestCase):
    def test_texto(self):
        self.assertIn("Hola", ch.preview_contenido("Hola deudor", "text"))

    def test_imagen(self):
        self.assertEqual(
            ch.preview_contenido("[Imagen recibida; presumiblemente comprobante de pago]", "image"),
            "📷 Imagen / comprobante",
        )

    def test_pdf(self):
        self.assertEqual(
            ch.preview_contenido("[Documento PDF enviado]", "document"),
            "📄 Documento PDF",
        )


class MediaMetadataTests(unittest.TestCase):
    def test_persiste_media_id_y_url_firmada(self):
        fake_bytes = b"\xff\xd8\xfffakejpeg"
        message = {
            "image": {
                "id": "MEDIA123",
                "mime_type": "image/jpeg",
                "caption": "comprobante",
            }
        }
        with patch.object(ch, "descargar_media_meta", return_value=(fake_bytes, "image/jpeg")):
            meta = ch.construir_metadata_media(message, "image")
        self.assertEqual(meta["media_id"], "MEDIA123")
        self.assertEqual(meta["mime_type"], "image/jpeg")
        self.assertIn("media_url", meta)
        self.assertIn("/control/media/", meta["media_url"])
        self.assertIn("token=", meta["media_url"])
        filename = meta["media_filename"]
        self.assertTrue((Path(ch._MEDIA_DIR) / filename).is_file())

    def test_sin_download_guarda_solo_media_id(self):
        message = {"image": {"id": "MEDIA999", "mime_type": "image/png"}}
        with patch.object(ch, "descargar_media_meta", return_value=(None, None)):
            meta = ch.construir_metadata_media(message, "image")
        self.assertEqual(meta["media_id"], "MEDIA999")
        self.assertNotIn("media_url", meta)


class FilaConversacionTests(unittest.TestCase):
    def test_expone_ultimo_mensaje_y_wa_id(self):
        nombres = [
            "id",
            "telefono",
            "identificacion",
            "estado",
            "modo_actual",
            "usuario_humano",
            "fecha_inicio",
            "fecha_ultima_actividad",
            "tomada_en",
            "metadata",
            "total_mensajes",
            "ultimo_contenido",
            "ultimo_tipo_mensaje",
        ]
        row = (
            1,
            "1442782103907655",
            "123",
            "ACTIVA",
            "AGENTE",
            None,
            None,
            None,
            None,
            {"wa_id": "573126487636", "psid": "1442782103907655"},
            3,
            "Buenas tardes",
            "text",
        )
        fila = ch._fila_conversacion(row, nombres)
        self.assertEqual(fila["ultimo_mensaje"], "Buenas tardes")
        self.assertEqual(fila["last_message"], "Buenas tardes")
        self.assertEqual(fila["phone"], "1442782103907655")  # clave API
        self.assertEqual(fila["wa_id"], "573126487636")
        self.assertEqual(fila["phone_legible"], "573126487636")
        self.assertEqual(fila["conversation_key"], "1442782103907655")
        self.assertEqual(fila["psid"], "1442782103907655")


if __name__ == "__main__":
    unittest.main()
