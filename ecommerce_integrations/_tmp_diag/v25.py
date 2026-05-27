def main():
    import frappe, importlib, json
    # Pick recent items from this run
    r = frappe.db.sql("""
      SELECT ei.erpnext_item_code, ei.integration_item_code, ei.pushed_image_map, ei.last_synced_at
      FROM `tabEcommerce Item` ei
      JOIN `tabItem` it ON it.item_code = ei.erpnext_item_code
      WHERE ei.integration='shopware6' AND ei.last_sync_run='SYNC-RUN-2026-22825'
        AND it.brand='Test GmbH'
      ORDER BY ei.last_synced_at DESC LIMIT 3
    """, as_dict=True)
    if not r:
        # any item
        r = frappe.db.sql("""
          SELECT erpnext_item_code, integration_item_code, pushed_image_map, last_synced_at
          FROM `tabEcommerce Item`
          WHERE integration='shopware6' AND last_sync_run='SYNC-RUN-2026-22825'
          ORDER BY last_synced_at DESC LIMIT 3
        """, as_dict=True)
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def check(client):
        ids = [x['integration_item_code'] for x in r]
        if not ids: 
            print("no synced items yet")
            return
        res = client.request_post("search/product", payload={
            "limit": len(ids),
            "filter":[{"type":"equalsAny","field":"id","value":ids}],
            "associations":{"properties":{}},
            "includes":{"product":["id","productNumber","name","deliveryTimeId","coverId","price","stock"],
                        "property_group_option":["id","name"]},
        })
        by_id = {p['id']:p for p in (res.get("data") or [])}
        for x in r:
            p = by_id.get(x['integration_item_code']) or {}
            mp = json.loads(x['pushed_image_map'] or '{}')
            price = p.get('price')
            price_val = (price[0].get('gross') if isinstance(price,list) and price else None)
            print(f"\n{x['erpnext_item_code']} ({x['integration_item_code'][:8]})")
            print(f"  productNumber: {p.get('productNumber')}")
            print(f"  name: {p.get('name')[:60] if p.get('name') else None}")
            print(f"  deliveryTimeId: {'set' if p.get('deliveryTimeId') else 'EMPTY'}")
            print(f"  coverId: {'set' if p.get('coverId') else 'EMPTY'}")
            print(f"  price (gross): {price_val}")
            print(f"  stock: {p.get('stock')}")
            print(f"  properties (count): {len(p.get('properties') or [])}")
            print(f"  pushed_image_map (count): {len(mp)}")
    mod._with_client(check)
