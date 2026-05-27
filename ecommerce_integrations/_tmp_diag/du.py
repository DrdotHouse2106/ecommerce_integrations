def main():
    import frappe, importlib, uuid
    # Get 5 different file_urls used by current items
    rows = frappe.db.sql("""
      SELECT DISTINCT f.file_url
      FROM `tabFile` f WHERE f.attached_to_doctype='Item' AND f.is_private=0 AND f.is_folder=0
      AND f.file_url IS NOT NULL AND f.file_url != ''
      LIMIT 5
    """, as_dict=True)
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    from ecommerce_integrations.product_sync.engine.payload import _build_shopware_media
    def test_one(client, file_url):
        media = _build_shopware_media([{"url": file_url}])
        if not media: return f"{file_url} → empty"
        abs_url = media[0]["media"]["url"]
        media_id = uuid.uuid4().hex
        try:
            client.request_post("_action/sync", payload={"media":{"entity":"media","action":"upsert","payload":[{"id":media_id}]}})
        except Exception as e: return f"{abs_url} CREATE: {e}"
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload={"url": abs_url},
                additional_query_params={"fileName":"test","extension":"jpg"},
            )
            r = "OK"
        except Exception as e:
            r = f"FAIL: {str(e)[:150]}"
        try: client.request_post("_action/sync", payload={"del":{"entity":"media","action":"delete","payload":[{"id":media_id}]}})
        except: pass
        return f"{abs_url[:80]} → {r}"
    def runner(client):
        for r in rows:
            print(test_one(client, r['file_url']))
    mod._with_client(runner)
