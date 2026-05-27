def main():
    import frappe
    # Clear local cache to get fresh read
    try: delattr(frappe.local, "_shopware_image_base")
    except: pass
    base = frappe.db.get_single_value("Shopware Setting", "image_public_base_url")
    print(f"DB setting: {base!r}")
    
    from ecommerce_integrations.product_sync.engine.payload import _resolve_image_public_base, _build_shopware_media
    resolved = _resolve_image_public_base()
    print(f"resolved: {resolved!r}")
    
    # Build a media URL with current config
    media = _build_shopware_media([{"url":"/file/abc/test.jpg"}])
    print(f"would send to Shopware: {media[0]['media']['url'] if media else None}")
