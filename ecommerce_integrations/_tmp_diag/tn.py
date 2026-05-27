def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    # Take a real product id + a known existing option_uuid
    pid = "019dc4c000007cf492cb8b2b2d1c7b30"  # SKU 78.489.090.A from earlier
    opt_uuid = "c2c69d8b0531eaff63e2cb56dc6f223e"  # "Höhenbereich: bis 1000 mm"
    
    def go(client):
        # Check current properties on product
        r = client.request_post("search/product", payload={
            "limit": 1,
            "filter":[{"type":"equals","field":"productNumber","value":"78.489.090.A"}],
            "associations":{"properties":{}},
            "includes":{"product":["id"],"property_group_option":["id","name"]},
        })
        p = (r.get("data") or [None])[0]
        if not p:
            print("not found")
            return
        actual_pid = p['id']
        print(f"actual product id: {actual_pid}")
        before = p.get("properties") or []
        print(f"BEFORE: {len(before)} properties")
        for x in before[:3]: print(f"  {x.get('name')}")
        
        # Send the m2m link via _action/sync
        print("\n--- POST _action/sync with nested properties ---")
        try:
            res = client.request_post("_action/sync", payload={
                "p": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": [{"id": actual_pid, "properties": [{"id": opt_uuid}]}],
                }
            })
            print(f"OK: {res}")
        except Exception as e:
            print(f"ERR: {str(e)[:400]}")
        
        # Re-check
        import time; time.sleep(2)
        r2 = client.request_post("search/product", payload={
            "limit": 1, "filter":[{"type":"equals","field":"id","value":actual_pid}],
            "associations":{"properties":{}},
            "includes":{"product":["id"],"property_group_option":["id","name"]},
        })
        after = ((r2.get("data") or [{}])[0]).get("properties") or []
        print(f"\nAFTER: {len(after)} properties")
        for x in after[:3]: print(f"  {x.get('name')}")
    mod._with_client(go)
