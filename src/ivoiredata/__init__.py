"""IvoireData Engine."""

__version__ = "0.8.5"

# Load discovery-refresh hardening before engine imports the base connector.
from .connectors import official_docs_refresh as _official_docs_refresh  # noqa: F401,E402

# Route canonical GitHub documentation trees through the zero-redownload Git connector.
from .connectors import official_docs_strategy as _official_docs_strategy  # noqa: F401,E402

# Add source-agnostic CURRENT_STABLE resolution for canonical Git documentation.
from .connectors import official_git_versions as _official_git_versions  # noqa: F401,E402

# Preserve canonical Git tree subdirectories and use rate-safe authenticated GitHub blob
# transport when credentials are configured.
from .connectors import official_git_hardening as _official_git_hardening  # noqa: F401,E402

# Observe every source sync in the physical Artifact Ledger without coupling connectors
# to SQLite or changing CI Gold semantics.
from . import artifact_runtime as _artifact_runtime  # noqa: F401,E402

# Register the Maven Central native metadata/POM authority for both CLI and library use.
from . import technology_maven_authority as _technology_maven_authority  # noqa: F401,E402
