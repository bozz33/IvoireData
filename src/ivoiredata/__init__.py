"""IvoireData Engine."""

__version__ = "0.8.3"

# Load the small discovery-refresh hardening before engine imports the base connector.
# This keeps sitemap/index freshness conditional and crash-safe without duplicating the
# main official-docs connector implementation.
from .connectors import official_docs_refresh as _official_docs_refresh  # noqa: F401,E402
