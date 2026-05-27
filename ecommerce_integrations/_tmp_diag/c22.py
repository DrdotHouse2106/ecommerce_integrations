def main():
    import frappe
    # how many ecom items synced via 22816
    n = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND last_sync_run='SYNC-RUN-2026-22816'
    """)[0][0]
    print(f"Items persisted via 22816: {n}")
    # sample of failed items? check result errors via run row
    r = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, items_failed,
             created_count, updated_count, error_summary
      FROM `tabEcommerce Sync Run` WHERE name='SYNC-RUN-2026-22816'
    """, as_dict=True)[0]
    print(r)
