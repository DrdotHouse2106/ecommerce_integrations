def main():
    import frappe
    r = frappe.db.sql("""
      SELECT LEFT(error_summary, 2000) FROM `tabEcommerce Sync Run` WHERE name='SYNC-RUN-2026-22828'
    """, as_dict=True)[0]
    print((r.get('LEFT(error_summary, 2000)') or '')[:2000])
