def main():
    import frappe
    runs = frappe.db.sql("SELECT name FROM `tabEcommerce Sync Run` WHERE status='running'", as_dict=True)
    for r in runs:
        frappe.db.set_value("Ecommerce Sync Run", r["name"], {
            "status": "error", "cancel_requested": 1,
            "error_summary": "cancelled — fetch_live=True hardcoded bug fix pending",
            "finished_at": frappe.utils.now_datetime(),
        })
    frappe.db.commit()
    print(f"cancelled {len(runs)}")
