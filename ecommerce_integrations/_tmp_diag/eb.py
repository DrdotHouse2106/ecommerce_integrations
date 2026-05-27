def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    
    # Build brand payload from ERP
    from ecommerce_integrations.product_sync.engine.payload import _resolve_image_public_base
    base = _resolve_image_public_base()
    print(f"public base: {base}")
    
    rows = frappe.get_all("Brand", fields=["name","image","description"])
    payloads = []
    for r in rows:
        img = (r.get("image") or "").strip()
        if img and not img.startswith(("http://","https://")) and base:
            img = base.rstrip("/") + (img if img.startswith("/") else "/" + img)
        payloads.append({
            "name": r["name"],
            "description": r.get("description") or "",
            "logo_url": img,
        })
    print(f"\n{len(payloads)} brands to push:")
    for p in payloads: print(f"  {p['name']:20} desc={bool(p['description'])} logo={p['logo_url'][:60] if p['logo_url'] else '-'}")
    
    # Push
    print("\n=== Calling ensure_brand_entities_bulk ===")
    adapter = mod.ShopwareProductAdapter()
    adapter.ensure_brand_entities_bulk(payloads)
    print("done.")
    
    # Verify
    print("\n=== Verifying on Shopware ===")
    from ecommerce_integrations.shopware6.export.utils import generate_uuid
    ids = [generate_uuid(f"manufacturer_{p['name']}") for p in payloads]
    def check(client):
        r = client.request_post("search/product-manufacturer", payload={
            "limit": len(ids),
            "filter":[{"type":"equalsAny","field":"id","value":ids}],
            "associations":{"media":{}},
        })
        by_id = {m["id"]:m for m in (r.get("data") or [])}
        for p, mid in zip(payloads, ids):
            m = by_id.get(mid) or {}
            media = m.get("media") or {}
            has_logo = bool(media.get("url"))
            print(f"  {p['name']:20} name={m.get('name','-')!r:30} mediaId={m.get('mediaId','-')[:8] if m.get('mediaId') else '-':10}  logo_url={'✓' if has_logo else '-'}")
    mod._with_client(check)
