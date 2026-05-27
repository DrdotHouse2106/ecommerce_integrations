def main():
    import importlib, uuid
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    media_id = uuid.uuid4().hex
    test_url = "http://10.10.0.6:8080/file/197d83843d/0130287-300-S10000_03.jpg"
    print(f"Testing URL: {test_url}")
    def test(client):
        try:
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        except Exception as e: print(f"create: {e}"); return
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": test_url},
                additional_query_params={"fileName":"iptest","extension":"jpg"},
            )
            print("✓ URL upload OK with private IP")
        except Exception as e:
            err = str(e)
            print(f"FAIL: {err[:400]}")
        finally:
            try: client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
            except: pass
    mod._with_client(test)
