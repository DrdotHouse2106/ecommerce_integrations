def main():
    import importlib, uuid
    from ecommerce_integrations.product_sync.engine.adapters.shopware import _fetch_local_file_bytes
    file_url = "/file/000304390a/32881-75_06.jpg"
    bytes_data, mime = _fetch_local_file_bytes(f"http://10.10.0.6:8089{file_url}")
    print(f"fetch result: bytes={len(bytes_data) if bytes_data else None}, mime={mime}")
    if not bytes_data:
        print("BYTES FETCH FAILED")
        return
    
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    media_id = uuid.uuid4().hex
    def test(client):
        try:
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        except Exception as e: print(f"create: {e}"); return
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload=bytes_data,
                content_type=mime or "image/jpeg",
                additional_query_params={"fileName":"bytestest","extension":"jpg"},
            )
            print("✓ BYTES upload OK")
        except Exception as e:
            print(f"✗ BYTES FAILED: {str(e)[:300]}")
        finally:
            try: client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
            except: pass
    mod._with_client(test)
