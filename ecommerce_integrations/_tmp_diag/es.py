def main():
    import frappe
    rows = frappe.db.sql("""
      SELECT name, item_code, severity, attempt_count, sync_run
      FROM `tabEcommerce Sync Error` 
      WHERE creation > '2026-05-26 15:31:00' ORDER BY creation DESC LIMIT 5
    """, as_dict=True)
    print(f"got {len(rows)} error rows")
    for r in rows:
        print(r)
    # check error message child table or full doc
    if rows:
        doc = frappe.get_doc("Ecommerce Sync Error", rows[0]['name'])
        print("\nFull doc fields:")
        for k in dir(doc):
            if not k.startswith('_'):
                v = getattr(doc, k, None)
                if isinstance(v, str) and v and len(v) > 10:
                    print(f"  {k}: {v[:200]}")
