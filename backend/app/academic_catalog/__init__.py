"""The reviewed, organization-scoped academic catalog.

The package deliberately does not import the admin router at module import
time.  Database model registration imports :mod:`app.academic_catalog.models`
from the shared metadata module, while the API imports :mod:`.admin` only when
the application router is assembled.
"""

__all__ = ["models", "schemas", "service"]
