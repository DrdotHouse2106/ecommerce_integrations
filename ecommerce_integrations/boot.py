from ecommerce_integrations.shopify.constants import OLD_SETTINGS_DOCTYPE


def boot_session(bootinfo):
	"""Don't show old doctypes after enabling new ones."""
	# Apply E-Invoice patch for PDF attachment
	try:
		from ecommerce_integrations.einvoice_patch import apply_patch
		apply_patch()
	except Exception:
		pass

	try:
		bootinfo.single_types.remove(OLD_SETTINGS_DOCTYPE)
	except ValueError:
		pass
