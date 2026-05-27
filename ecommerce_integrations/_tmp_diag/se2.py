def main():
    import frappe
    val = frappe.db.get_value("Ecommerce Sync Run", "SYNC-RUN-2026-22828", "error_summary")
    print((val or "")[:2000])
