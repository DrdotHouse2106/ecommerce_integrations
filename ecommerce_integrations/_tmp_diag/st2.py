def main():
    import frappe
    r = frappe.db.sql("""
      SELECT name, status, items_total, items_succeeded, items_failed,
             created_count, updated_count,
             TIMESTAMPDIFF(SECOND, started_at, IFNULL(finished_at, NOW())) AS dur
      FROM `tabEcommerce Sync Run` WHERE name LIKE 'SYNC-RUN-2026-2282%'
      ORDER BY creation DESC LIMIT 1
    """, as_dict=True)[0]
    can = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND last_synced_canonical IS NOT NULL AND last_synced_canonical != ''
    """)[0][0]
    print(f"run {r['name']}: {r['status']} tot={r['items_total']} suc={r['items_succeeded']} fail={r['items_failed']} dur={r['dur']}s")
    print(f"items with stored canonical: {can}")
