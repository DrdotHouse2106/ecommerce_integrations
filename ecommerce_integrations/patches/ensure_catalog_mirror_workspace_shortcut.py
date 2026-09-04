"""Idempotent patch: ensure the "Catalog Mirror" shortcut exists on the
"Ecommerce Integrations" workspace.

Frappe's workspace fixture sync only *creates* a workspace when none
exists yet on the site — on a site where "Ecommerce Integrations"
already existed before this shortcut was added to the shipped
fixture, ``bench migrate`` silently leaves the live workspace as-is
(protecting whatever the operator customised), so the shortcut never
appears without manually editing the workspace. Fresh installs don't
need this patch: there the workspace doesn't exist yet, so the normal
fixture sync creates it with the shortcut already included.
"""

import frappe

_WORKSPACE = "Ecommerce Integrations"
_LINK_TO = "Ecommerce Catalog Mirror"


def execute():
	if not frappe.db.exists("DocType", "Workspace"):
		return
	if not frappe.db.exists("Workspace", _WORKSPACE):
		return

	doc = frappe.get_doc("Workspace", _WORKSPACE)
	if any((row.link_to or "") == _LINK_TO for row in (doc.shortcuts or [])):
		return

	doc.append(
		"shortcuts",
		{
			"color": "Orange",
			"doc_view": "List",
			"label": "Catalog Mirror",
			"link_to": _LINK_TO,
			"type": "DocType",
		},
	)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
