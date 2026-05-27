def main():
    import frappe, importlib
    # Pick a recent uploaded image basename to find a real File
    f = frappe.db.sql("""
      SELECT name, file_name, file_url FROM `tabFile` 
      WHERE attached_to_doctype='Item' AND is_private=0 AND is_folder=0 
        AND file_url IS NOT NULL AND file_url != ''
      LIMIT 1
    """, as_dict=True)
    if not f: 
        print("no files")
        return
    file_url = f[0]['file_url']
    print(f"sample file_url: {file_url}")
    # Build absolute
    from ecommerce_integrations.product_sync.engine.payload import _build_shopware_media
    media = _build_shopware_media([{"url": file_url}])
    print(f"shopware_media: {media}")
    if media:
        abs_url = media[0].get("media", {}).get("url")
        print(f"absolute URL Shopware would fetch: {abs_url}")

    # Now try the actual URL upload to see what error
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    import uuid
    media_id = uuid.uuid4().hex
    def test(client):
        try:
            client.request_post("_action/sync", payload={
                "media": {"entity":"media","action":"upsert","payload":[{"id": media_id}]},
            })
            print(f"created media {media_id}")
        except Exception as e:
            print(f"create failed: {e}")
            return
        try:
            res = client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": abs_url},
                additional_query_params={"fileName":"test","extension":"jpg"},
            )
            print(f"URL upload OK: {res}")
        except Exception as e:
            print(f"URL upload FAILED: {type(e).__name__}: {str(e)[:300]}")
        finally:
            # cleanup
            try:
                client.request_post("_action/sync", payload={
                    "del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
            except: pass
    mod._with_client(test)
