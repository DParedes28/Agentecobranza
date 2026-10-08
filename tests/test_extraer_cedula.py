"""Guardrails P0: parser de cédula anti-monto/teléfono y anclaje de sesión."""

from __future__ import annotations

import unittest

from agente_cobranzas import (
    cedula_explicita_en_texto,
    clasificacion_imagen_respuesta,
    debe_reportar_abono,
    es_celular_co,
    extraer_cedula,
    sesion_identidad_anclada,
    texto_desde_imagen_meta,
)


class ExtraerCedulaMontosTests(unittest.TestCase):
    def test_monto_colombiano_con_punto_no_es_cedula(self):
        self.assertIsNone(extraer_cedula("$745.901"))
        self.assertIsNone(extraer_cedula("El saldo es $745.901 pesos"))

    def test_monto_us_con_coma_no_es_cedula(self):
        self.assertIsNone(extraer_cedula("$745,901"))
        self.assertIsNone(extraer_cedula("total 1,250,000"))

    def test_monto_con_palabra_pesos(self):
        self.assertIsNone(extraer_cedula("debo 745901 pesos"))


class ExtraerCedulaTelefonoTests(unittest.TestCase):
    def test_celular_co_10_digitos(self):
        self.assertTrue(es_celular_co("3234179599"))
        self.assertIsNone(extraer_cedula("tel 3234179599"))
        self.assertIsNone(extraer_cedula("mi whatsapp es 3234179599"))

    def test_celular_con_prefijo_57(self):
        self.assertTrue(es_celular_co("573234179599"))
        self.assertIsNone(extraer_cedula("llame al 573234179599"))


class ExtraerCedulaValidasTests(unittest.TestCase):
    def test_cedula_simple(self):
        self.assertEqual(extraer_cedula("mi documento 42126647"), "42126647")

    def test_cedula_explicita_frase(self):
        self.assertEqual(cedula_explicita_en_texto("mi cédula es 42126647"), "42126647")
        self.assertEqual(extraer_cedula("mi cédula es 42126647"), "42126647")
        self.assertEqual(extraer_cedula("CC 9872330"), "9872330")
        self.assertEqual(extraer_cedula("CC: 10.234.567"), "10234567")

    def test_cedula_con_puntos_de_formato_documento(self):
        # Con frase explícita, puntos de documento sí se aceptan.
        self.assertEqual(extraer_cedula("cédula 52.145.678"), "52145678")


class SesionAncladaTests(unittest.TestCase):
    def test_sesion_anclada_no_se_pisa_con_telefono(self):
        state = {
            "identidad_confirmada": True,
            "cedula": "42126647",
            "inmueble_id": 99,
        }
        self.assertTrue(sesion_identidad_anclada(state))
        self.assertEqual(
            extraer_cedula(
                "llame al 3234179599",
                sesion_cedula="42126647",
                identidad_confirmada=True,
            ),
            "42126647",
        )

    def test_sesion_anclada_no_se_pisa_con_monto(self):
        self.assertEqual(
            extraer_cedula(
                "el saldo es $745.901",
                sesion_cedula="42126647",
                identidad_confirmada=True,
            ),
            "42126647",
        )

    def test_sesion_anclada_acepta_cedula_explicita_nueva(self):
        self.assertEqual(
            extraer_cedula(
                "mi cédula es 9872330",
                sesion_cedula="42126647",
                identidad_confirmada=True,
            ),
            "9872330",
        )


class ImagenClassificationGateTests(unittest.TestCase):
    def test_caption_meta_pasa_a_texto(self):
        msg = {"image": {"id": "m1", "caption": "comprobante del apto 201"}}
        self.assertEqual(texto_desde_imagen_meta(msg), "comprobante del apto 201")

    def test_sin_caption_no_presume_comprobante(self):
        texto = texto_desde_imagen_meta({"image": {"id": "m1"}})
        self.assertNotIn("presumiblemente", texto.lower())
        self.assertIn("Clasifica", texto)

    def test_reportar_abono_solo_si_comprobante(self):
        carta = (
            "[CLASIFICACION_IMAGEN: carta-cobro]\n"
            "[ACCION: REPORTAR_ABONO | Monto=100000 | Fecha=2026-10-01 | Banco=Bancolombia | Ref=1]"
        )
        self.assertEqual(clasificacion_imagen_respuesta(carta), "carta-cobro")
        self.assertFalse(debe_reportar_abono(carta))

        ok = (
            "[CLASIFICACION_IMAGEN: comprobante]\n"
            "[ACCION: REPORTAR_ABONO | Monto=100000 | Fecha=2026-10-01 | Banco=Bancolombia | Ref=1]"
        )
        self.assertTrue(debe_reportar_abono(ok))

    def test_reportar_abono_sin_clasificacion_se_bloquea(self):
        raw = "[ACCION: REPORTAR_ABONO | Monto=100000 | Fecha=2026-10-01 | Banco=X | Ref=1]"
        self.assertFalse(debe_reportar_abono(raw))


class PromptPolicyGuardrailsTests(unittest.TestCase):
    def test_prompt_incluye_reglas_p0(self):
        from prompt_policy import SYSTEM_PROMPT

        self.assertIn("Nunca trates un número telefónico como cédula", SYSTEM_PROMPT)
        self.assertIn("ESCALAR_HUMANO", SYSTEM_PROMPT)
        self.assertIn("CLASIFICACION_IMAGEN", SYSTEM_PROMPT)
        self.assertIn("Prohibido prometer", SYSTEM_PROMPT)
        self.assertIn("SESION ACTIVA", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
