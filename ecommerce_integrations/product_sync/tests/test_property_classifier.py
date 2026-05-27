"""``property_priority`` — classifies an ``Item Ecommerce Property``
name into a Shopware ``property_group.position`` slot.

The classifier is shared between the apply pipeline (sets
``position`` on every group it ensures) and any one-off ERPNext-side
cleanup scripts. Both rely on the same priority so the order
visible in ERPNext matches the order rendered on the storefront PDP.
"""

from __future__ import annotations

import unittest

from ecommerce_integrations.product_sync.engine.property_classifier import (
    property_priority,
)


class TestPropertyPriority(unittest.TestCase):
    def test_hauptmasse_hit_priority_100(self):
        for name in ("Breite", "Länge", "Höhe", "Tiefe", "Gewicht", "Durchmesser"):
            self.assertEqual(property_priority(name), 100, name)
            self.assertEqual(property_priority(f"{name} (mm)"), 100, name)

    def test_bereich_hit_priority_150(self):
        for name in ("Breitenbereich", "Höhenbereich", "Tiefenbereich"):
            self.assertEqual(property_priority(name), 150, name)

    def test_material_hit_priority_200(self):
        for name in ("Material", "Oberfläche", "Bauweise", "RAL-Nummer"):
            self.assertEqual(property_priority(name), 200, name)

    def test_farbe_hit_priority_300(self):
        for name in ("Farbe", "Grundfarbe", "Farbton",
                     "Farbe Fachboden / Auflage"):
            self.assertEqual(property_priority(name), 300, name)

    def test_aufnahme_hit_priority_700(self):
        for name in ("Aufnahme-Art", "Aufnahme-Innenhöhe",
                     "Aufnahme-Position"):
            self.assertEqual(property_priority(name), 700, name)

    def test_versand_hit_priority_900(self):
        for name in ("Versand-Breite", "Versand-Höhe", "Versand-Länge"):
            self.assertEqual(property_priority(name), 900, name)

    def test_unmatched_falls_back_to_sonstige(self):
        """Unknown names get priority 800 (between explicit
        categories and the legacy Aufnahme/Versand buckets) so they
        appear after the named groups but before the legacy ones."""
        self.assertEqual(property_priority("Eigenschaft die wir nicht kennen"), 800)
        self.assertEqual(property_priority("Foobar Whatever"), 800)

    def test_priorities_are_monotonic_within_category_blocks(self):
        """Sanity: the Hauptmaße block (100s) should come before
        Material (200s), Material before Farbe (300s), etc. — guards
        against accidental priority swap during edits."""
        self.assertLess(property_priority("Höhe"), property_priority("Material"))
        self.assertLess(property_priority("Material"), property_priority("Farbe"))
        self.assertLess(property_priority("Farbe"), property_priority("Ausstattung"))
        self.assertLess(property_priority("Ausstattung"), property_priority("Aufnahme-Art"))
        self.assertLess(property_priority("Aufnahme-Art"), property_priority("Versand-Breite"))


if __name__ == "__main__":
    unittest.main()
