def main():
    import frappe
    # How many items have pushed_image_map populated
    r = frappe.db.sql("""
      SELECT 
        SUM(CASE WHEN pushed_image_map IS NULL OR pushed_image_map='' OR pushed_image_map='{}' THEN 1 ELSE 0 END) as empty,
        SUM(CASE WHEN pushed_image_map IS NOT NULL AND pushed_image_map != '' AND pushed_image_map != '{}' THEN 1 ELSE 0 END) as populated
      FROM `tabEcommerce Item`
      WHERE integration='shopware6' AND integration_item_code IS NOT NULL
    """, as_dict=True)[0]
    print(f"map empty: {r['empty']} | populated: {r['populated']}")
