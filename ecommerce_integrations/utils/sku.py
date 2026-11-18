"""Product-number helpers shared by the backend integrations."""


def sku_variants(sku: str) -> list[str]:
    """``sku`` plus its spelling with the other decimal separator.

    Shops sometimes store a product number with a decimal comma while the
    ERP item code uses a dot (``956737,5`` vs ``956737.5``) or vice versa;
    matching both spellings links the existing product instead of creating
    a duplicate."""
    sku = (sku or "").strip()
    if not sku:
        return []
    out = [sku]
    for a, b in ((",", "."), (".", ",")):
        if a in sku:
            out.append(sku.replace(a, b))
    return out
