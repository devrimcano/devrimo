"""Credential-local SAIS adapter with admission before every HTTP request.

The worker uses the same vendored parser shipped in the MCP image. Loading it
under a private package avoids importing upstream configuration or passing a
database credential into a student's MCP process.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

from app.campus.credentials import CampusSecrets

_PACKAGE = "app.academic_catalog._source_vendor"


def client_type():
    cached = sys.modules.get(f"{_PACKAGE}.sais_client")
    if cached is not None:
        return cached.SAISClient
    vendor = Path(__file__).resolve().parents[2] / "vendor_patches" / "course_info"
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(vendor)]
    sys.modules[_PACKAGE] = package
    config = types.ModuleType(f"{_PACKAGE}.config")
    config.settings = types.SimpleNamespace(sais_username="", sais_password="", locale="tr")
    sys.modules[config.__name__] = config
    try:
        for name in ("models", "sais_client"):
            spec = importlib.util.spec_from_file_location(f"{_PACKAGE}.{name}", vendor / f"{name}.py")
            if spec is None or spec.loader is None:
                raise RuntimeError("The packaged SAIS parser is unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        return sys.modules[f"{_PACKAGE}.sais_client"].SAISClient
    except Exception:
        for name in list(sys.modules):
            if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
                sys.modules.pop(name, None)
        raise


class CatalogSource:
    def __init__(self, secrets: CampusSecrets, request_gate):
        self.client = client_type()(
            username=secrets.metu_username,
            password=secrets.metu_password,
            locale=secrets.locale,
            request_gate=request_gate,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def read(self, tool: str, values: dict[str, str]) -> Any:
        allowed = {
            "get_departments_and_semesters", "list_program_courses", "get_course_info",
            "get_section_constraints", "get_course_prerequisites", "get_course_replacements",
            "get_thesis_courses",
        }
        if tool not in allowed:
            raise ValueError("Unsupported catalog ingestion tool")
        arguments = {
            {"department": "department_code", "semester": "semester_code", "course": "course_code"}.get(k, k): v
            for k, v in values.items()
        }
        value = await getattr(self.client, tool)(**arguments)

        def plain(item):
            if hasattr(item, "model_dump"):
                return item.model_dump(mode="json")
            if isinstance(item, list):
                return [plain(child) for child in item]
            return item

        return plain(value)
