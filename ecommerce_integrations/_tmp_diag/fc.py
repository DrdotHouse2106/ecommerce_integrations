def main():
    import frappe, importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    
    # 1. All items with stored canonical
    can = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND last_synced_canonical IS NOT NULL
    """)[0][0]
    print(f"Items with stored canonical: {can}/37982")
    
    # 2. Run summary
    r = frappe.db.sql("""
      SELECT items_total, items_succeeded, items_failed, updated_count,
             TIMESTAMPDIFF(SECOND, started_at, finished_at) AS dur
      FROM `tabEcommerce Sync Run` WHERE name='SYNC-RUN-2026-218943'
    """, as_dict=True)[0]
    print(f"Run 218943: total={r['items_total']} success={r['items_succeeded']} fail={r['items_failed']} updated={r['updated_count']} dur={r['dur']}s")
    
    # 3. Property distribution analysis
    print("\n=== Property name distribution ===")
    rows = frappe.db.sql("""
      SELECT property_name, COUNT(*) cnt
      FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      GROUP BY property_name ORDER BY cnt DESC LIMIT 30
    """, as_dict=True)
    print(f"Distinct active property names: {frappe.db.sql('SELECT COUNT(DISTINCT property_name) FROM `tabItem Ecommerce Property` WHERE parenttype=\"Item\" AND sync_to_shopware=1')[0][0]}")
    for r in rows:
        print(f"  {r['property_name']:30} {r['cnt']:>6}x")
    
    print("\n=== Booleans / false / null values ===")
    fv = frappe.db.sql("""
      SELECT property_name, property_value, COUNT(*) cnt
      FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND (property_value IN ('false','0','no','nein','-','') OR property_value IS NULL)
      GROUP BY property_name, property_value
      ORDER BY cnt DESC LIMIT 10
    """, as_dict=True)
    for r in fv:
        print(f"  {r['property_name']:30} = {r['property_value']!r:15} {r['cnt']:>6}x")
