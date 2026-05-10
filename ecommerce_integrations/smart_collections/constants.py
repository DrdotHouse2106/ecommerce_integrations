"""Shared identifiers for the Smart Collections module.

Centralises backend names and Shopware visibility-level mappings so the
strings live in one place — adding a new backend or changing a label only
touches this module.
"""


BACKEND_MEDUSA = "Medusa"
BACKEND_SHOPWARE = "Shopware"

KNOWN_BACKENDS = (BACKEND_MEDUSA, BACKEND_SHOPWARE)


# Shopware visibility levels keyed by the option label rendered on
# ``Ecommerce Smart Collection Target.visibility`` Select. The numeric
# value is the Shopware ``product_visibility.visibility`` integer; ``0``
# means "not visible at all" and adapters skip channels at that level.
VISIBILITY_LEVELS: dict[str, int] = {
    "All (30)": 30,
    "Linked Only (20)": 20,
    "Search Only (10)": 10,
    "Hidden (0)": 0,
}

VISIBILITY_DEFAULT = "All (30)"
