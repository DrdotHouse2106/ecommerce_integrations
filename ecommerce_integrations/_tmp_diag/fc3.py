def main():
    import frappe, importlib, json
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    
    # 1. Current run status
    r = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, items_failed,
             created_count, updated_count
      FROM `tabEcommerce Sync Run` WHERE name='SYNC-RUN-2026-22827'
    """, as_dict=True)[0]
    print(f"22827: {r['status']} | tot={r['items_total']} suc={r['items_succeeded']} fail={r['items_failed']}")
    
    # 2. Canonical store: how many items have it set
    n = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND last_synced_canonical IS NOT NULL
        AND last_synced_canonical != ''
    """)[0][0]
    print(f"items with stored canonical: {n}")
    
    # 3. Media populated since worker restart 18:41
    def q(client):
        emp = client.request_post("search-ids/media", payload={
            "limit":1, "total-count-mode":1,
            "filter":[
                {"type":"range","field":"createdAt","parameters":{"gte":"2026-05-26T18:41:00"}},
                {"type":"equals","field":"fileName","value":None},
            ],
        })
        pop = client.request_post("search-ids/media", payload={
            "limit":1, "total-count-mode":1,
            "filter":[
                {"type":"range","field":"createdAt","parameters":{"gte":"2026-05-26T18:41:00"}},
                {"type":"not","operator":"and","queries":[{"type":"equals","field":"fileName","value":None}]},
            ],
        })
        print(f"media since restart: empty={emp.get('total')} populated={pop.get('total')}")
    mod._with_client(q)
