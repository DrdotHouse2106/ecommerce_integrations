def main():
    import frappe, importlib
    # Run status
    r = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, items_failed,
             TIMESTAMPDIFF(SECOND, started_at, IFNULL(finished_at, NOW())) AS dur
      FROM `tabEcommerce Sync Run`
      WHERE name='SYNC-RUN-2026-218945'
    """, as_dict=True)[0]
    print(f"218945: {r['status']} | tot={r['items_total']} suc={r['items_succeeded']} fail={r['items_failed']} dur={r['dur']}s")
    
    # Verify positions on Shopware
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    from ecommerce_integrations.product_sync.engine.property_classifier import property_priority
    
    # Sample 10 distinct property names from ERP
    names = frappe.db.sql("""
      SELECT DISTINCT property_name FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      ORDER BY property_name LIMIT 10
    """, pluck="property_name")
    
    from ecommerce_integrations.shopware6.export.utils import generate_uuid
    def go(client):
        for n in names:
            expected = property_priority(n)
            gid = generate_uuid(f"property_group_{n}")
            r = client.request_post("search/property-group", payload={
                "limit":1, "filter":[{"type":"equals","field":"id","value":gid}],
                "includes":{"property_group":["id","name","position"]},
            })
            d = (r.get("data") or [None])[0] or {}
            actual = d.get("position")
            match = "✓" if actual == expected else "✗"
            print(f"  {match} {n:35} expected={expected:>3}  actual={actual}")
    mod._with_client(go)
