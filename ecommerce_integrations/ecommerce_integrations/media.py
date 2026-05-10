"""Cross-channel media constants.

Single source of truth for image-extension matching so the image-sync
hook, the Shopware delta-sync, and the Medusa product export agree on
what counts as an image. Adding a new format means editing one tuple.
"""


IMAGE_EXTENSIONS: tuple[str, ...] = (
	".jpg",
	".jpeg",
	".png",
	".gif",
	".webp",
	".bmp",
	".avif",
)


def is_image_url(file_url: str | None) -> bool:
	"""Return True iff ``file_url`` ends in a known image extension."""
	if not file_url:
		return False
	return file_url.lower().endswith(IMAGE_EXTENSIONS)
