def main():
    import importlib, uuid, time
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    test_url = "http://10.10.0.6:8089/file/000304390a/32881-75_06.jpg"
    
    media_ids = []
    print("Burst 20 URL uploads (mimicking worker rate)...")
    for i in range(20):
        mid = uuid.uuid4().hex
        media_ids.append(mid)
        def go(client, m=mid, idx=i):
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":m}]}})
            try:
                client.request_post(f"_action/media/{m}/upload",
                    payload={"url": test_url},
                    additional_query_params={"fileName":f"r{idx}","extension":"jpg"})
            except Exception as e:
                print(f"  call {idx}: ERR {str(e)[:100]}")
        mod._with_client(go)
    
    print("Wait 10s for async fetches...")
    time.sleep(10)
    
    def verify(client):
        r = client.request_post("search/media", payload={
            "limit": 25,
            "filter":[{"type":"equalsAny","field":"id","value":media_ids}],
            "includes":{"media":["id","fileName","url"]},
        })
        full = sum(1 for m in (r.get("data") or []) if m.get('url'))
        empty = sum(1 for m in (r.get("data") or []) if not m.get('url'))
        print(f"Result: populated={full} empty={empty}")
        # cleanup
        client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":x} for x in media_ids]}})
    mod._with_client(verify)
