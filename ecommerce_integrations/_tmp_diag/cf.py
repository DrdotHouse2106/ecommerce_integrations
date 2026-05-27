def main():
    import frappe
    print(frappe.db.has_column("Brand", "last_synced_brand_hash"))
