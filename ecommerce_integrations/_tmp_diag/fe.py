def main():
    import frappe, importlib, uuid
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    media_id = uuid.uuid4().hex
    test_url = "http://erp.internal:8080/file/197d83843d/0130287-300-S10000_03.jpg"
    def test(client):
        try:
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        except Exception as e: print(f"create: {e}"); return
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": test_url},
                additional_query_params={"fileName":"x","extension":"jpg"},
            )
            print("OK")
        except Exception as e:
            print(f"ERROR FULL: {str(e)}")
        finally:
            try: client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
            except: pass
    mod._with_client(test)
