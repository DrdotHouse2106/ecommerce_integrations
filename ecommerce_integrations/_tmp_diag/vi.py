def main():
    import frappe, importlib, json
    # Find an item that's been recently synced (last_sync_run=22820)
    r = frappe.db.sql("""
      SELECT erpnext_item_code, integration_item_code, pushed_image_map, last_synced_at
      FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND last_sync_run='SYNC-RUN-2026-22820'
      ORDER BY last_synced_at DESC LIMIT 3
    """, as_dict=True)
    if not r:
        print("no items via 22820 found yet")
        return
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def check(client):
        for x in r:
            pid = x['integration_item_code']
            map_d = json.loads(x['pushed_image_map'] or '{}')
            print(f"\n=== {x['erpnext_item_code']} ({pid}) pushed_map={len(map_d)} items ===")
            # Get product_media + media
            res = client.request_post("search/product-media", payload={
                "limit": 20,
                "filter":[{"type":"equals","field":"productId","value":pid}],
                "associations":{"media":{}},
            })
            for pm in (res.get("data") or []):
                m = pm.get('media') or {}
                print(f"  pm={pm.get('id')[:8]} mediaId={pm.get('mediaId')[:8]} mediaFn={m.get('fileName')} url={'set' if m.get('url') else 'EMPTY'} folder={'set' if m.get('mediaFolderId') else 'EMPTY'}")
    mod._with_client(check)
