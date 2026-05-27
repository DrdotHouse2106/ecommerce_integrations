def main():
    import frappe
    r = frappe.db.sql("""
      SELECT items_total, items_succeeded, items_failed, created_count, updated_count,
             error_summary
      FROM `tabEcommerce Sync Run` WHERE name='SYNC-RUN-2026-22816'
    """, as_dict=True)[0]
    print(f"tot={r['items_total']} suc={r['items_succeeded']} fail={r['items_failed']} c={r['created_count']} u={r['updated_count']}")
    print("\n=== error_summary (first 2000 chars) ===")
    print((r.get('error_summary') or '')[:2000])
