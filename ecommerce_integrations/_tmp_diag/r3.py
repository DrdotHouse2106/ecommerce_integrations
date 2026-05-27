def main():
    import frappe
    rows = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, created_count, updated_count, items_failed,
             TIMESTAMPDIFF(SECOND, started_at, IFNULL(finished_at, NOW())) AS dur
      FROM `tabEcommerce Sync Run`
      WHERE creation > DATE_SUB(NOW(), INTERVAL 5 MINUTE)
      ORDER BY creation DESC LIMIT 5
    """, as_dict=True)
    for r in rows:
        print(f"{r['name']} | {r['status']:9} | tot={r['items_total']} suc={r['items_succeeded']} c={r['created_count']} u={r['updated_count']} f={r['items_failed']} | {r['dur']}s")
