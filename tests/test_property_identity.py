"""Tests unitarios sin acceso a Neon para el parser de inmuebles."""

import unittest

from property_identity import extract_property_reference


class PropertyReferenceTests(unittest.TestCase):
    def test_torre_apto(self):
        self.assertEqual(
            extract_property_reference("quiero saber la deuda de la torre 25 apto 203"),
            {"torre": "25", "apto": "203"},
        )

    def test_apartamento(self):
        self.assertEqual(
            extract_property_reference("estado de cuenta Torre 25 Apartamento 203"),
            {"torre": "25", "apto": "203"},
        )

    def test_pair_with_debt_intent(self):
        self.assertEqual(
            extract_property_reference("cual es la deuda del 25 203"),
            {"torre": "25", "apto": "203"},
        )

    def test_hyphen_with_debt_intent(self):
        self.assertEqual(
            extract_property_reference("saldo 25-203"),
            {"torre": "25", "apto": "203"},
        )

    def test_plain_id_not_property(self):
        self.assertIsNone(extract_property_reference("mi cedula es 42151328"))


if __name__ == "__main__":
    unittest.main()
