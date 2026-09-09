"""How the department course list identifies itself, and why that mattered.

Every catalog import this project has run has failed, and the reviewed catalog
that the admin panel reads has stayed empty, because of one guard: the course
list page was required to carry its department's numeric code. It does not
carry it anywhere. The page prints

    Department : Mathematics/Matematik   Semester : 20261   Code Name ECTS ...

and the returned form holds no department control at all, so the check found
nothing, compared nothing to "236", and rejected the answer - for all 207 of
METU's departments, every time.

The fixtures below are the real strings, including the part that makes this
harder than it looks: METU serves the selector and the result page under
different encodings, so the same programme is "Havacılık" in one and
"Havac?l?k" in the other.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

VENDOR = Path(__file__).resolve().parents[1] / "vendor_patches" / "course_info"


@pytest.fixture(scope="module")
def sais():
    package = types.ModuleType("_vendored_listing")
    package.__path__ = [str(VENDOR)]
    sys.modules["_vendored_listing"] = package

    config = types.ModuleType("_vendored_listing.config")
    config.settings = types.SimpleNamespace(sais_username="u", sais_password="p", locale="tr")
    sys.modules["_vendored_listing.config"] = config

    for name in ("models", "sais_client"):
        spec = importlib.util.spec_from_file_location(f"_vendored_listing.{name}", VENDOR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    yield sys.modules["_vendored_listing.sais_client"]

    for name in [key for key in sys.modules if key.startswith("_vendored_listing")]:
        del sys.modules[name]


def listing_page(department_name: str, semester: str = "20261") -> BeautifulSoup:
    """The shape SAIS returns: a heading, then the course table."""
    return BeautifulSoup(
        f"<html><body><p>Department : {department_name} Semester : {semester}</p>"
        "<table><tr><th>Code</th><th>Name</th></tr>"
        "<tr><td>2360111</td><td>FUNDAMENTALS OF MATHEMATICS</td></tr></table>"
        "</body></html>",
        "html.parser",
    )


def test_the_page_is_accepted_when_its_name_is_the_one_we_asked_for(sais):
    sais._verify_course_response_identity(
        listing_page("Mathematics/Matematik"),
        None,
        "20261",
        department_code="236",
        department_label="Mathematics/Matematik",
    )


def test_a_page_naming_another_department_is_still_rejected(sais):
    """The guard's whole purpose: never store one department's list as another's."""
    with pytest.raises(ValueError, match="department identity"):
        sais._verify_course_response_identity(
            listing_page("Physics/Fizik"),
            None,
            "20261",
            department_code="236",
            department_label="Mathematics/Matematik",
        )


def test_two_programmes_that_share_a_name_are_still_told_apart(sais):
    """METU lists Aerospace Engineering twice; only one is the Cyprus campus."""
    ankara = "Aerospace Engineering/Havacılık ve Uzay Mühendisliği"
    cyprus = "Aerospace Engineering/Havacılık ve Uzay Mühendisliği(Kuzey Kıbrıs Kampüsü)"
    sais._verify_course_response_identity(
        listing_page(cyprus), None, "20261", department_code="384", department_label=cyprus
    )
    with pytest.raises(ValueError, match="department identity"):
        sais._verify_course_response_identity(
            listing_page(ankara), None, "20261", department_code="384", department_label=cyprus
        )


def test_the_two_encodings_of_one_name_are_treated_as_the_same_name(sais):
    """The selector's spelling and the result page's spelling, as METU sends them."""
    from_selector = "Aerospace Engineering/Havacılık ve Uzay Mühendisliği(Kuzey Kıbrıs Kampüsü)"
    from_result_page = "Aerospace Engineering/Havac�l�k ve Uzay M�hendisli�i(Kuzey K�br�s Kamp�s�)"
    assert sais._ascii_skeleton(from_selector) == sais._ascii_skeleton(from_result_page)
    sais._verify_course_response_identity(
        listing_page(from_result_page), None, "20261",
        department_code="384", department_label=from_selector,
    )


def test_a_department_the_course_app_does_not_serve_reads_as_empty(sais):
    """METU's own sentence for Actuarial Science and Applied Ethics.

    Both are in the department directory and neither has a course list. This
    was the first department in the plan, so a whole-school import died on step
    one and the other 206 were never fetched.
    """
    soup = BeautifulSoup(
        "<html><body><p>Information about the department could not be found. "
        "This program lists the offered courses on department and semester basis."
        "</p></body></html>",
        "html.parser",
    )
    assert sais._explicit_empty_result(soup, "course") is True
