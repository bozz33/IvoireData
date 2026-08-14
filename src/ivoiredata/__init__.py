"""IvoireData Engine."""

__version__ = "0.8.3"

# Load discovery-refresh hardening before engine imports the base connector.
from .connectors import official_docs_refresh as _official_docs_refresh  # noqa: F401,E402

# Route canonical GitHub documentation trees through the zero-redownload Git connector
# while preserving the historical `official_docs` connector interface.
from .connectors import official_docs_strategy as _official_docs_strategy  # noqa: F401,E402

# Finally add non-blocking CURRENT_STABLE resolution for canonical Git documentation.
from .connectors import official_git_versions as _official_git_versions  # noqa: F401,E402
