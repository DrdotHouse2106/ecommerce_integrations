def main():
    import importlib, uuid, time
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    media_id = uuid.uuid4().hex
    test_url = "http://10.10.0.6:8089/file/000304390a/32881-75_06.jpg"
    def go(client):
        client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": test_url},
                additional_query_params={"fileName":"verify","extension":"jpg"},
            )
            print("upload call returned OK")
        except Exception as e:
            print(f"upload call: {e}")
            return
        time.sleep(3)
        # Read back
        r = client.request_post("search/media", payload={
            "limit":1, "filter":[{"type":"equals","field":"id","value":media_id}],
            "includes":{"media":["id","fileName","url","mimeType","mediaType"]},
        })
        m = (r.get("data") or [{}])[0]
        print(f"after upload: fn={m.get('fileName')!r} url={m.get('url')!r} mimeType={m.get('mimeType')}")
        # cleanup
        client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
    mod._with_client(go)
