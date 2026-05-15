"""CategoryAdapter implementations register themselves into the
``base._REGISTRY`` at import time via the ``@register(backend)``
decorator. Import the concrete adapter modules here so the registry is
populated on package import — otherwise ``tasks.sync_collection`` calls
``get_adapter("Medusa")`` and gets ``No adapter registered for backend
'Medusa'`` because the module that would have registered it was never
imported.
"""

from ecommerce_integrations.smart_collections.engine.adapters import (
	medusa,
	shopware,
)
