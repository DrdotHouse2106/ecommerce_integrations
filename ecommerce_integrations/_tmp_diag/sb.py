def main():
    import frappe
    frappe.db.set_value("Shopware Setting", "Shopware Setting", "image_public_base_url", "http://10.10.0.6:8089")
    frappe.db.commit()
    # Clear cached value on frappe.local
    try: delattr(frappe.local, "_shopware_image_base")
    except: pass
    val = frappe.db.get_single_value("Shopware Setting", "image_public_base_url")
    print(f"set image_public_base_url = {val!r}")
