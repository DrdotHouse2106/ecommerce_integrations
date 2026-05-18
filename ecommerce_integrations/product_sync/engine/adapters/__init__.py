"""Backend-specific ProductAdapter implementations.

The base ABC lives in :mod:`.base`; one module per backend implements
it. The orchestrator never imports these directly — it goes through
:func:`product_sync.engine.registry.get_adapter`.
"""
