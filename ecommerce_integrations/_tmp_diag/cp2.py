def main():
    import frappe
    for code in ['105001', '105002']:
        rows = frappe.get_all("Item Ecommerce Property",
            filters={"parent": code, "parenttype":"Item", "sync_to_shopware": 1},
            fields=["property_name","property_value"])
        print(f"{code}: {len(rows)} ecommerce_properties → {rows[:3]}")
    # Sample item with actual props
    rows = frappe.db.sql("""
      SELECT parent, COUNT(*) cnt FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      GROUP BY parent ORDER BY cnt DESC LIMIT 3
    """, as_dict=True)
    print(f"\nTop items with most properties:")
    for r in rows: print(r)
