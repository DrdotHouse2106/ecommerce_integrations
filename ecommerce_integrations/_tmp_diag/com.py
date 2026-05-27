def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        # The fileName 12950-130-LR-B_02 from morning sync
        r = client.request_post("search/media", payload={
            "limit": 3,
            "filter":[{"type":"equals","field":"fileName","value":"12950-130-LR-B_02"}],
            "includes":{"media":["id","fileName","url","createdAt","metaData","mediaFolderId"]},
        })
        for m in (r.get("data") or []):
            print(f"morning media: {m}")
    mod._with_client(q)
