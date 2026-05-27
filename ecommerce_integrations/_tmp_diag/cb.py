def main():
    import frappe
    cfs = frappe.get_all("Custom Field", filters={"dt":"Brand"}, fields=["fieldname","fieldtype"])
    print(f"Brand custom fields: {cfs}")
    # Sample brands
    brands = frappe.db.sql("SELECT brand, image, LEFT(description, 100) desc FROM `tabBrand` LIMIT 5", as_dict=True)
    print(f"\nSample brands ({len(brands)}):")
    for b in brands: print(f"  {b['brand']!r} image={b['image']!r} desc={b['desc']!r}")
    # Total brands
    n = frappe.db.sql("SELECT COUNT(*) FROM `tabBrand`")[0][0]
    print(f"\nTotal Brand records: {n}")
    # Distinct brands used by Items
    n2 = frappe.db.sql("SELECT COUNT(DISTINCT brand) FROM `tabItem` WHERE brand IS NOT NULL AND brand != ''")[0][0]
    print(f"Distinct brands used by Items: {n2}")
