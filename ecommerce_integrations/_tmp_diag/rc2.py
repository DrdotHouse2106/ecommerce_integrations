def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    pid = "27ff38f1c8dde3d489938983ef1ba775"
    def go(client):
        # Read product expanded with properties (no includes filter — let it return all)
        r = client.request_post("search/product", payload={
            "limit": 1,
            "filter":[{"type":"equals","field":"id","value":pid}],
            "associations":{"properties":{"associations":{"group":{}}}},
        })
        p = (r.get("data") or [None])[0]
        if not p:
            print("not found")
            return
        props = p.get("properties") or []
        print(f"product.properties (via associations): {len(props)}")
        for pp in props[:5]:
            g = pp.get("group") or {}
            print(f"  {g.get('name')}: {pp.get('name')}")
    mod._with_client(go)
