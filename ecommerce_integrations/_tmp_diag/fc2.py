def main():
    import frappe
    r = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, items_failed, 
             created_count, updated_count, deactivated_count, modified,
             preview_plan_json IS NULL AS plan_empty,
             LENGTH(preview_plan_json) AS plan_size
      FROM `tabEcommerce Sync Run`
      WHERE name = 'SYNC-RUN-2026-22813'
    """, as_dict=True)
    print(r[0] if r else "not found")
