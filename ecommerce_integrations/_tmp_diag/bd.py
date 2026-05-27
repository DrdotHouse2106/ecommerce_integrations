def main():
    import frappe
    fields = frappe.db.sql("SHOW COLUMNS FROM `tabBrand`", as_dict=True)
    for f in fields[:15]:
        print(f"  {f['Field']:30} {f['Type']}")
    # Sample brand
    sample = frappe.db.sql("""
      SELECT brand, description, brand_defaults FROM `tabBrand` LIMIT 3
    """, as_dict=True) if any(f['Field'] in ('description','brand_defaults') for f in fields) else []
    print(f"\nSample brands:")
    for s in sample: print(s)
