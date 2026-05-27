def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # Get recent media without filter
        r = client.request_post("search/media", payload={
            "limit": 10,
            "sort":[{"field":"createdAt","order":"DESC"}],
            "includes":{"media":["id","fileName","url","createdAt"]},
        })
        for m in (r.get("data") or []):
            url = m.get('url') or ''
            fn = m.get('fileName') or ''
            print(f"  {(fn[:40] or '<no-name>'):42} {m.get('createdAt','')[:19]} url={url[:60]}")
    mod._with_client(q)
