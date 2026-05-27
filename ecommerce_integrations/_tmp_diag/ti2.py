def main():
    import importlib, uuid
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    media_id = uuid.uuid4().hex
    test_url = "http://10.10.0.6:8089/file/197d83843d/0130287-300-S10000_03.jpg"
    print(f"Testing URL: {test_url}")
    def test(client):
        try:
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        except Exception as e: print(f"create: {e}"); return
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": test_url},
                additional_query_params={"fileName":"porttest","extension":"jpg"},
            )
            print("✓ URL upload OK with port 8089")
        except Exception as e:
            print(f"FAIL: {str(e)[:300]}")
        finally:
            try: client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
            except: pass
    mod._with_client(test)
