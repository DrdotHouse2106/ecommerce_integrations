def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # Get a few existing media to see their URLs/origin
        r = client.request_post("search/media", payload={
            "limit": 5,
            "filter":[{"type":"contains","field":"fileName","value":""}],
            "includes":{"media":["id","fileName","url","createdAt"]},
        })
        for m in (r.get("data") or []):
            print(f"  {m.get('fileName'):50} created={m.get('createdAt')[:19]} url={m.get('url')}")
    mod._with_client(q)
