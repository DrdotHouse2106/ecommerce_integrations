def main():
    import frappe
    brands = frappe.db.sql("SELECT brand, image FROM `tabBrand` LIMIT 5", as_dict=True)
    print(f"Sample brands:")
    for b in brands: print(f"  {b['brand']!r}  image={b['image']!r}")
    n = frappe.db.sql("SELECT COUNT(*) FROM `tabBrand`")[0][0]
    print(f"\nTotal Brand records: {n}")
    n2 = frappe.db.sql("SELECT COUNT(DISTINCT brand) FROM `tabItem` WHERE brand IS NOT NULL AND brand != ''")[0][0]
    print(f"Distinct brands used by Items: {n2}")
    # Brands with image
    n3 = frappe.db.sql("SELECT COUNT(*) FROM `tabBrand` WHERE image IS NOT NULL AND image != ''")[0][0]
    print(f"Brands with image: {n3}")
