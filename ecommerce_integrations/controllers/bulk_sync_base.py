"""Cross-channel primitives for bulk-sync queues.

Each integration (shopware6, medusa, rag, …) keeps its own queue
because the work units differ — products, property maps, vector
embeddings — but they all share the same lock-acquisition,
bulk-mode-detection and request-frequency mechanics. Calling these
helpers from each channel instead of re-rolling them per module keeps
the lock semantics *atomic* and consistent.

The previous per-channel ``acquire_sync_lock`` implementations did a
non-atomic ``get → set`` against the cache: under load two workers
could both observe "no lock", both write, and both proceed as if
they held the lock. The :func:`acquire_lock` helper here issues a
single Redis ``SET … NX EX`` command — atomic by construction.

Keep this module thin and dependency-free (just ``frappe``). Channel
modules import what they need; there is no Queue *class* here because
the on-the-wire payloads differ enough (json-list of item codes vs.
json-list of dicts vs. raw Redis set) that a single abstraction would
either pretend the differences away or grow as many knobs as the
existing duplication.
"""

import time
from collections.abc import Iterable

import frappe
from frappe.utils import cint


def acquire_lock(key: str, ttl_seconds: int = 600) -> bool:
	"""Atomically acquire a bulk-sync lock.

	Returns ``True`` iff the caller now holds the lock. The lock auto-expires
	after ``ttl_seconds`` so a crashed worker can never permanently wedge
	the channel.

	Uses Redis ``SET NX EX`` directly. Both this and :func:`release_lock`
	operate on the raw key (no Frappe site-prefix): the channel-specific
	prefix (``shopware6:``, ``medusa:``, ``rag:``) is part of the key the
	caller passes in, which matches what the pre-refactor implementations
	stored, so existing keys keep working.
	"""
	return bool(frappe.cache.set(key, "1", nx=True, ex=ttl_seconds))


def release_lock(key: str) -> None:
	"""Release a previously acquired lock.

	Uses raw ``delete`` so it operates on the same key namespace as
	:func:`acquire_lock`; ``delete_value`` would target a Frappe-prefixed
	key that the atomic SET never wrote to. Best-effort: deleting a key
	that's already gone is fine.
	"""
	frappe.cache.delete(key)


class BulkModeTracker:
	"""Frequency-based bulk-mode detection for queue-backed integrations.

	Counts operations against ``request_count_key`` inside a sliding window
	of ``window`` seconds. When the threshold is crossed, ``bulk_mode_key``
	gets set with TTL ``cooldown * 3``; queue processors check that key and
	defer until it expires, so a flurry of edits batches into one sync run.

	The bulk-mode key's TTL is also refreshed on every call while it's
	already active, so the cooldown clock effectively resets with each new
	operation — bulk mode only lifts once activity has been quiet for at
	least ``cooldown`` seconds.

	Each channel passes its own ``key_prefix`` (e.g. ``"shopware6"``,
	``"medusa"``) so multiple channels track their bulk modes independently.
	"""

	def __init__(
		self,
		key_prefix: str,
		threshold: int = 5,
		window: int = 2,
		cooldown: int = 10,
	):
		self.bulk_mode_key = f"{key_prefix}:bulk_mode"
		self.request_count_key = f"{key_prefix}:request_count"
		self.last_request_key = f"{key_prefix}:last_request"
		self.threshold = threshold
		self.window = window
		self.cooldown = cooldown

	def is_active(self) -> bool:
		return bool(frappe.cache.get_value(self.bulk_mode_key))

	def activate(self) -> None:
		frappe.cache.set_value(self.bulk_mode_key, "1", expires_in_sec=self.cooldown * 3)

	def deactivate(self) -> None:
		frappe.cache.delete_value(self.bulk_mode_key)

	def should_activate(self) -> bool:
		"""Register one operation; return True iff bulk mode should be on.

		If bulk mode is already active, refresh its TTL and return ``True``
		without touching the counter — there's no need to recount once we're
		batching.

		Otherwise, increment the in-window counter. When it crosses the
		threshold, activate bulk mode and return ``True``.
		"""
		cache = frappe.cache
		if cache.get_value(self.bulk_mode_key):
			cache.set_value(self.bulk_mode_key, "1", expires_in_sec=self.cooldown * 3)
			return True

		now = time.time()
		last = cache.get_value(self.last_request_key)
		count = cint(cache.get_value(self.request_count_key)) or 0
		if last and now - float(last) > self.window:
			count = 0
		count += 1
		cache.set_value(self.request_count_key, str(count), expires_in_sec=self.window * 2)
		cache.set_value(self.last_request_key, str(now), expires_in_sec=self.window * 2)

		if count >= self.threshold:
			self.activate()
			return True
		return False


def unique_extend(target: list, items: Iterable) -> list:
	"""Append ``items`` to ``target`` skipping anything already present.

	Returned for clarity; ``target`` is also mutated in place. Used by the
	channel-specific queues that store the queue as a JSON list and need
	cheap deduplication on add.
	"""
	seen = set(target)
	for item in items:
		if item not in seen:
			target.append(item)
			seen.add(item)
	return target
