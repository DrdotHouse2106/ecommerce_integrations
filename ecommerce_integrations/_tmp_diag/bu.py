def main():
    import importlib, uuid, time
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    test_url = "http://10.10.0.6:8089/file/000304390a/32881-75_06.jpg"
    ids = []
    def burst(client):
        for i in range(5):
            mid = uuid.uuid4().hex
            ids.append(mid)
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":mid}]}})
            try:
                client.request_post(
                    f"_action/media/{mid}/upload",
                    payload={"url": test_url},
                    additional_query_params={"fileName":f"burst{i}","extension":"jpg"},
                )
                print(f"  call {i}: 204")
            except Exception as e:
                print(f"  call {i} FAILED: {str(e)[:120]}")
        time.sleep(5)
        # Verify
        r = client.request_post("search/media", payload={
            "limit": 5,
            "filter":[{"type":"equalsAny","field":"id","value":ids}],
            "includes":{"media":["id","fileName","url"]},
        })
        for m in (r.get("data") or []):
            print(f"  {m.get('id')[:8]} fn={m.get('fileName')!r} url={'OK' if m.get('url') else 'EMPTY'}")
        # cleanup
        client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":i} for i in ids]}})
    mod._with_client(burst)
