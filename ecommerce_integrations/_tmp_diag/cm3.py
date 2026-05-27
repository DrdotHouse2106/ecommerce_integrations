def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # Media WITH a real fileName, sorted by createdAt
        r = client.request_post("search/media", payload={
            "limit": 5,
            "filter":[{"type":"not","operator":"or","queries":[{"type":"equals","field":"fileName","value":None}]}],
            "sort":[{"field":"createdAt","order":"DESC"}],
            "includes":{"media":["id","fileName","url","createdAt","mediaFolderId"]},
        })
        for m in (r.get("data") or []):
            url = m.get('url') or ''
            fn = m.get('fileName') or ''
            print(f"  fn={fn} | created={m.get('createdAt','')[:19]} | url={url[:80]} | folder={m.get('mediaFolderId')}")
    mod._with_client(q)
