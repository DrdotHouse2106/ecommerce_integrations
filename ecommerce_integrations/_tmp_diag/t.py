def main():
    import frappe
    from ecommerce_integrations.product_sync.scheduler import _enqueue_sync
    _enqueue_sync("Shopware Testlauf")
    frappe.db.commit()
    print("enqueued")
