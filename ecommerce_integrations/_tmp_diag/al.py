def main():
    import frappe, os, base64
    mapping = {
        "Test GmbH":   ("brandA.png",   "image/png"),
        "Brand_G":          ("cp.svg",      "image/svg+xml"),
        "brand_d":        ("brand_d.svg",   "image/svg+xml"),
        "Brand_E":       ("brand_e.png",  "image/png"),
        "BRAND_C":        ("brand_c.svg",   "image/svg+xml"),
        "Brand_F":      ("brand_f.png", "image/png"),
        "BRAND_B":      ("brand_b.svg", "image/svg+xml"),
    }
    for brand_name, (fn, mime) in mapping.items():
        path = f"/tmp/brand_logos/{fn}"
        if not os.path.exists(path):
            print(f"  SKIP {brand_name} — file missing")
            continue
        with open(path, "rb") as fh:
            content = fh.read()
        # Create File doc attached to Brand
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": f"{brand_name.replace(' ','_').replace('+','plus')}_logo.{fn.rsplit('.',1)[1]}",
            "attached_to_doctype": "Brand",
            "attached_to_name": brand_name,
            "is_private": 0,
            "content": content,
        })
        file_doc.flags.ignore_permissions = True
        try:
            file_doc.insert()
            # Set Brand.image
            frappe.db.set_value("Brand", brand_name, "image", file_doc.file_url)
            print(f"  ✓ {brand_name}: {file_doc.file_url}")
        except Exception as e:
            # Already exists?
            existing = frappe.db.get_value("File", {
                "attached_to_doctype": "Brand",
                "attached_to_name": brand_name,
                "is_folder": 0,
            }, "file_url")
            if existing:
                frappe.db.set_value("Brand", brand_name, "image", existing)
                print(f"  ✓ {brand_name}: reused existing {existing}")
            else:
                print(f"  ✗ {brand_name}: {e}")
    frappe.db.commit()
