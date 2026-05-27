def main():
    import frappe, importlib, json
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    # Sample: 1 non-Bauer item, 1 Bauer variant, 1 Bauer template
    samples = []
    for filt, label in [
        ({"brand": "Test GmbH", "has_variants": 1, "disabled": 0}, "Bauer-Template"),
        ({"brand": "Test GmbH", "variant_of": ("is", "set"), "disabled": 0}, "Bauer-Variant"),
        ({"brand": ("!=", "Test GmbH"), "disabled": 0}, "Non-Bauer"),
    ]:
        rows = frappe.db.sql("""
          SELECT it.item_code, it.has_variants, it.variant_of, ei.integration_item_code, ei.last_sync_run
          FROM `tabItem` it JOIN `tabEcommerce Item` ei
            ON ei.erpnext_item_code=it.item_code AND ei.integration='shopware6'
          WHERE it.brand=%s AND it.disabled=0 AND it.has_variants=%s
            AND ei.integration_item_code IS NOT NULL
            AND ei.last_synced_canonical IS NOT NULL
          LIMIT 1
        """, (filt.get("brand") if isinstance(filt.get("brand"), str) else ("Test GmbH"),
              1 if "has_variants" in filt and filt["has_variants"] == 1 else 0),
          as_dict=True) if "has_variants" in filt and filt["has_variants"] == 1 else []
        if not rows:
            # Generic search
            rows = frappe.db.sql("""
              SELECT it.item_code, it.has_variants, it.variant_of,
                     ei.integration_item_code, ei.last_sync_run
              FROM `tabItem` it JOIN `tabEcommerce Item` ei
                ON ei.erpnext_item_code=it.item_code AND ei.integration='shopware6'
              WHERE ei.last_synced_canonical IS NOT NULL
                AND ei.integration_item_code IS NOT NULL
              ORDER BY ei.last_synced_at DESC LIMIT 1
            """, as_dict=True)
        if rows:
            samples.append((label, rows[0]))
    
    print(f"=== Verifying {len(samples)} sample items ===\n")
    ids = [s[1]['integration_item_code'] for s in samples]
    def go(client):
        res = client.request_post("search/product", payload={
            "limit": len(ids),
            "filter":[{"type":"equalsAny","field":"id","value":ids}],
            "associations":{
                "properties":{},
                "options":{},
                "configuratorSettings":{},
                "media":{},
            },
            "includes":{"product":["id","productNumber","name","deliveryTimeId","coverId","price","stock","manufacturer"]},
        })
        by_id = {p["id"]: p for p in (res.get("data") or [])}
        for label, r in samples:
            p = by_id.get(r['integration_item_code'], {})
            mp_count = len(p.get("media") or [])
            props_count = len(p.get("properties") or [])
            opts_count = len(p.get("options") or [])
            cfg_count = len(p.get("configuratorSettings") or [])
            price = p.get("price")
            price_val = (price[0].get("gross") if isinstance(price, list) and price else None)
            print(f"--- {label}: {r['item_code']} → {p.get('productNumber')} ---")
            print(f"  name:        {(p.get('name') or '')[:50]}")
            print(f"  deliveryT:   {'✓' if p.get('deliveryTimeId') else 'EMPTY'}")
            print(f"  cover:       {'✓' if p.get('coverId') else 'EMPTY'}")
            print(f"  price:       {price_val}")
            print(f"  media:       {mp_count} rows")
            print(f"  properties:  {props_count}")
            print(f"  options:     {opts_count} (variant)")
            print(f"  configSetts: {cfg_count} (template)")
            print(f"  last_run:    {r['last_sync_run']}")
    mod._with_client(go)
