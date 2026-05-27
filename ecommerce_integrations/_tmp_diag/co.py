def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    # Look up properties for item 78.489.090.A
    code = "78.489.090.A"
    eprops = frappe.get_all("Item Ecommerce Property",
        filters={"parent": code, "parenttype":"Item", "sync_to_shopware": 1},
        fields=["property_name","property_value"])
    print(f"ERP ecommerce_properties for {code}: {len(eprops)}")
    for e in eprops[:5]:
        print(f"  - {e['property_name']}: {e['property_value']}")
    
    # Check if property_group exists in Shopware for one of them
    if not eprops: return
    sample_name = eprops[0]['property_name']
    from ecommerce_integrations.shopware6.export.utils import generate_uuid
    expected_group_uuid = generate_uuid(f"property_group_{sample_name}")
    expected_opt_uuid = generate_uuid(f"property_option_{sample_name}_{eprops[0]['property_value']}")
    print(f"\nLooking up:")
    print(f"  group_uuid: {expected_group_uuid}")
    print(f"  option_uuid: {expected_opt_uuid}")
    def q(client):
        g = client.request_post("search/property-group", payload={
            "limit":1, "filter":[{"type":"equals","field":"id","value":expected_group_uuid}],
            "includes":{"property_group":["id","name"]},
        })
        gd = (g.get("data") or [None])[0]
        print(f"  group in SW: {gd}")
        o = client.request_post("search/property-group-option", payload={
            "limit":1, "filter":[{"type":"equals","field":"id","value":expected_opt_uuid}],
            "includes":{"property_group_option":["id","name","groupId"]},
        })
        od = (o.get("data") or [None])[0]
        print(f"  option in SW: {od}")
    mod._with_client(q)
