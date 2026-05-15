"""Ecommerce Smart Collection main doctype."""

import re
import unicodedata

from frappe.model.document import Document

_GERMAN_UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
    }
)


def slugify(value: str) -> str:
    """Lowercase URL-safe handle. Maps German umlauts before ASCII fold so
    ``Büros`` becomes ``bueros`` instead of ``bros``."""
    if not value:
        return ""
    value = value.translate(_GERMAN_UMLAUT_MAP)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value


class EcommerceSmartCollection(Document):
    def autoname(self) -> None:
        # ``autoname=field:slug`` reads ``self.slug`` to produce ``self.name``.
        # Populate it from the title here so users don't have to type it twice.
        if not self.slug and self.title:
            self.slug = slugify(self.title)
