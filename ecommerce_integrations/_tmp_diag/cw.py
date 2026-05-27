def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # most recent 5 media (created in last 5min)
        r = client.request_post("search/media", payload={
            "limit": 10,
            "sort":[{"field":"createdAt","order":"DESC"}],
            "includes":{"media":["id","fileName","url","createdAt"]},
        })
        for m in (r.get("data") or []):
            fn = m.get('fileName') or '<empty>'
            url_ok = bool(m.get('url'))
            print(f"  {m.get('createdAt','')[:19]}  fn={fn[:40]}  url={'OK' if url_ok else 'EMPTY'}")
    mod._with_client(q)
