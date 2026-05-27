def main():
    import frappe
    brands = frappe.db.sql("SELECT brand FROM `tabBrand` ORDER BY brand", pluck="brand")
    for b in brands: print(b)
