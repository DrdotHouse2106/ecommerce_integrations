def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    # 3 samples
    rows = frappe.db.sql("""
      SELECT it.item_code, it.has_variants, it.variant_of, ei.integration_item_code
      FROM `tabItem` it JOIN `tabEcommerce Item` ei
        ON ei.erpnext_item_code=it.item_code AND ei.integration='shopware6'
      WHERE ei.last_sync_run='SYNC-RUN-2026-218943'
        AND ei.integration_item_code IS NOT NULL
      ORDER BY RAND() LIMIT 5
    """, as_dict=True)
    def go(client):
        for r in rows:
            pid = r['integration_item_code']
            res = client.request_post("search/product", payload={
                "limit": 1, "filter":[{"type":"equals","field":"id","value":pid}],
                "associations":{
                    "properties":{},
                    "options":{},
                    "configuratorSettings":{},
                    "media":{},
                },
            })
            p = (res.get("data") or [None])[0] or {}
            tag = f"var(of={r['variant_of']})" if r['variant_of'] else ("tpl" if r['has_variants'] else "single")
            print(f"{r['item_code']:24} [{tag:18}] dt={'✓' if p.get('deliveryTimeId') else '✗'} cov={'✓' if p.get('coverId') else '✗'} props={len(p.get('properties') or [])} opts={len(p.get('options') or [])} cfg={len(p.get('configuratorSettings') or [])} media={len(p.get('media') or [])}")
    mod._with_client(go)
