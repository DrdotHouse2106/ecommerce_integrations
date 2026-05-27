def main():
    import importlib, uuid, time
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    test_url = "http://10.10.0.6:8089/file/000304390a/32881-75_06.jpg"
    
    # Test 1: WITHOUT shared session
    print("=== Test A: WITHOUT shared_shopware_session ===")
    media_id_a = uuid.uuid4().hex
    def go_a(client):
        client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id_a}]}})
        client.request_post(f"_action/media/{media_id_a}/upload",
            payload={"url": test_url},
            additional_query_params={"fileName":"noshared","extension":"jpg"})
    mod._with_client(go_a)
    
    # Test 2: WITH shared session (mimics what worker did)
    print("=== Test B: WITH shared_shopware_session ===")
    media_id_b = uuid.uuid4().hex
    with mod.shared_shopware_session() as sh_client:
        # Now _with_client should short-circuit to sh_client
        def go_b(client):
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id_b}]}})
            client.request_post(f"_action/media/{media_id_b}/upload",
                payload={"url": test_url},
                additional_query_params={"fileName":"withshared","extension":"jpg"})
        mod._with_client(go_b)
    
    # Verify both after a delay
    time.sleep(4)
    def verify(client):
        r = client.request_post("search/media", payload={
            "limit":2,
            "filter":[{"type":"equalsAny","field":"id","value":[media_id_a, media_id_b]}],
            "includes":{"media":["id","fileName","url"]},
        })
        for m in (r.get("data") or []):
            tag = "A (no-shared)" if m["id"]==media_id_a else "B (shared)"
            print(f"  {tag}: fn={m.get('fileName')!r} url={'OK' if m.get('url') else 'EMPTY'}")
        # cleanup
        client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id_a},{"id":media_id_b}]}})
    mod._with_client(verify)
