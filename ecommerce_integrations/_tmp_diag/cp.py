def main():
    import frappe, importlib
    canon_mod = importlib.import_module("ecommerce_integrations.product_sync.engine.canonical")
    pay_mod = importlib.import_module("ecommerce_integrations.product_sync.engine.payload")
    code = "78.489.090.A"
    item = frappe.get_doc("Item", code)
    sync = frappe.get_doc("Ecommerce Product Sync", "Shopware Testlauf")
    canonical = canon_mod.build_canonical_payload(item, sync)
    print(f"canonical.properties.ecommerce_properties count: {len(canonical.get('properties',{}).get('ecommerce_properties') or [])}")
    payload = pay_mod.build_shopware_payload(item, sync, canonical, external_id="test", changed_sections=None)
    print(f"payload keys: {sorted(payload.keys())}")
    print(f"payload.properties count: {len(payload.get('properties') or [])}")
    if payload.get('properties'):
        print(f"  sample: {payload['properties'][:3]}")
