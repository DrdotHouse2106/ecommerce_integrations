"""Helpers for associating Medusa product images with their specific variants.

After a multi-variant product is exported, each variant needs its image
URL linked to the variant id. The mapping goes via the Medusa
``/products/{id}/images/{image_id}/variants/batch`` endpoint.

These functions used to live inline in ``product_export.py``; lifting
them out keeps the exporter focused on the build-and-upload flow and
makes the image-association policy individually testable.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe

from ecommerce_integrations.medusa.connection import medusa_request
from ecommerce_integrations.medusa.constants import API_PRODUCTS


def collect_variant_image_jobs(product: dict, variant_image_map: dict) -> list:
	"""Collect ``(product_id, image_id, variant_ids)`` tuples for parallel execution.

	Groups by image so each tuple becomes one API call via
	``POST /products/{id}/images/{image_id}/variants/batch``.
	"""
	product_id = product.get("id")
	if not product_id:
		return []

	url_to_image_id = {}
	for img in product.get("images", []):
		if img.get("url") and img.get("id"):
			url_to_image_id[img["url"]] = img["id"]

	if not url_to_image_id:
		return []

	image_to_variants = {}
	for variant in product.get("variants", []):
		sku = variant.get("sku")
		variant_id = variant.get("id")
		if not sku or not variant_id or sku not in variant_image_map:
			continue
		image_url = variant_image_map[sku]
		image_id = url_to_image_id.get(image_url)
		if image_id:
			image_to_variants.setdefault(image_id, []).append(variant_id)

	return [(product_id, image_id, vids) for image_id, vids in image_to_variants.items()]


def associate_variant_images(session, base_url, product: dict, variant_image_map: dict):
	"""Associate product images with their specific variants (single-product, sequential).

	Used by single-product export. For bulk sync, use :func:`run_variant_image_jobs`.
	"""
	for product_id, image_id, variant_ids in collect_variant_image_jobs(product, variant_image_map):
		try:
			endpoint = f"{API_PRODUCTS}/{product_id}/images/{image_id}/variants/batch"
			medusa_request(session, base_url, "POST", endpoint, json={"add": variant_ids})
		except Exception as e:
			frappe.log_error("Medusa Variant Images", f"Failed for image {image_id}: {e}")


def run_variant_image_jobs(session, base_url, jobs: list, max_workers: int = 5):
	"""Execute variant-image association jobs in parallel.

	Each job is a ``(product_id, image_id, variant_ids)`` tuple. Uses
	threads to overlap network I/O — the AdaptiveThrottle is thread-safe.
	"""
	if not jobs:
		return

	def _do_one(product_id, image_id, variant_ids):
		endpoint = f"{API_PRODUCTS}/{product_id}/images/{image_id}/variants/batch"
		medusa_request(session, base_url, "POST", endpoint, json={"add": variant_ids})

	with ThreadPoolExecutor(max_workers=max_workers) as pool:
		futures = {
			pool.submit(_do_one, pid, iid, vids): (pid, iid)
			for pid, iid, vids in jobs
		}
		for future in as_completed(futures):
			pid, iid = futures[future]
			try:
				future.result()
			except Exception as e:
				frappe.log_error("Medusa Variant Images", f"Failed for product {pid} image {iid}: {e}")
