def main():
    import frappe
    val = frappe.db.get_single_value("Shopware Setting", "image_public_base_url")
    print(f"image_public_base_url: {val!r}")
    site_url = frappe.utils.get_url()
    print(f"frappe.utils.get_url(): {site_url!r}")
