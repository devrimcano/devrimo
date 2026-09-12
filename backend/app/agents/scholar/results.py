"""Tool results are projected before they are bounded.

Every result is re-sent on each later model step, and the 6,000-character bound
was turning real reads into a truncated preview: on a live prerequisites read,
3,948 of 5,692 characters were the fields the answer used, and other catalog
reads crossed the bound entirely - the model then reported that the list "came
back shortened" and answered from the front half. The `_catalog` block alone
(release ids, components, registration window) was 1,744 characters of that
payload and is never cited in an answer.

Projection is generic on purpose: it removes private (underscore) keys and null
values, recursively, and changes nothing else. It never guesses which domain
fields an answer needs.
"""

from typing import Any


def project(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: project(item)
            for key, item in value.items()
            if item is not None and not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [project(item) for item in value]
    return value
