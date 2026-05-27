def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    pid = "27ff38f1c8dde3d489938983ef1ba775"  # SKU 78.489.090.A
    def go(client):
        # Direct query of product_property join table
        r = client.request_post("search/product-property", payload={
            "limit": 50,
            "filter":[{"type":"equals","field":"productId","value":pid}],
        })
        print(f"product_property rows for {pid}: {r.get('total')}")
        # First few
        for d in (r.get("data") or [])[:5]:
            print(f"  optionId={d.get('optionId')} productId={d.get('productId')}")
        
        # Same via /api/product/{id}/properties endpoint
        try:
            r2 = client.request_post(f"search/property-group-option", payload={
                "limit":50,
                "filter":[{"type":"equals","field":"productProperties.productId","value":pid}],
                "includes":{"property_group_option":["id","name","groupId"]},
            })
            print(f"\noptions linked to product (via reverse): {r2.get('total')}")
            for d in (r2.get("data") or [])[:5]:
                print(f"  {d.get('name')} (group {d.get('groupId')[:8] if d.get('groupId') else None})")
        except Exception as e:
            print(f"reverse search failed: {e}")
    mod._with_client(go)
