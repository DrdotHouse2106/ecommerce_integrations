"""Ecommerce Smart Collection Snapshot.

Caches the resolver's last item-set per ``(collection, target_idx)`` pair.
Stored as gzip+base64 JSON in a single Long Text column so a site with
50 collections × 10k items lands in low single-digit MB instead of half a
million child-table rows.
"""

import base64
import gzip
import json

from frappe.model.document import Document


class EcommerceSmartCollectionSnapshot(Document):
    def set_items(self, item_codes) -> None:
        codes = sorted(set(item_codes or ()))
        payload = json.dumps(codes, separators=(",", ":")).encode("utf-8")
        self.item_codes_gz = base64.b64encode(gzip.compress(payload)).decode("ascii")
        self.item_count = len(codes)

    def get_items(self) -> set[str]:
        if not self.item_codes_gz:
            return set()
        raw = gzip.decompress(base64.b64decode(self.item_codes_gz))
        return set(json.loads(raw))
