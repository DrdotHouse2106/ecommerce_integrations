def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # Find product 105001 by productNumber
        r = client.request_post("search/product", payload={
            "limit": 1,
            "filter":[{"type":"equals","field":"productNumber","value":"105001"}],
            "associations":{"properties":{"associations":{"group":{}}}},
        })
        p = (r.get("data") or [None])[0]
        if not p:
            print("not found")
            return
        print(f"product id: {p.get('id')}")
        print(f"name: {p.get('name')}")
        props = p.get("properties") or []
        print(f"properties count: {len(props)}")
        for pr in props[:5]:
            g = pr.get("group") or {}
            print(f"  - {g.get('name')}: {pr.get('name')}")
    mod._with_client(q)
