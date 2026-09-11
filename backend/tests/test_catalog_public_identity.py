import pytest
from datetime import UTC, datetime, timedelta

from app.academic_catalog.service import _parse_observation, _prior_grade, _section_constraint_metadata
from app.academic_catalog.models import CatalogCourseRevision, CatalogSection


@pytest.mark.parametrize("field,value", [("course_code", "2300201"), ("semester", "20252"), ("section", "2")])
def test_wrong_source_identity_cannot_certify_requested_section(field, value):
    payload = {"course_code": "2402201", "semester": "20261", "section": "1", "constraints": []}
    payload[field] = value
    candidate, issues, status = _parse_observation("get_section_constraints", {
        "course": "2402201", "semester": "20261", "section": "1", "department": "240",
    }, payload)
    assert candidate == {}
    assert status == "malformed"
    assert issues[0]["code"] == "source_identity_mismatch"


def test_prior_grade_requires_the_same_department_and_full_course_number():
    assert _prior_grade([{"course_code": "2300201", "grade": "AA"}], "2402201") is None
    assert _prior_grade([{"course_code": "2301213", "grade": "AA"}], "2300213") is None
    assert _prior_grade([{"course_code": "PHYS 213", "grade": "BB"}], "2300213") == "BB"


def test_fresh_other_section_does_not_renew_old_section_constraints():
    now = datetime.now(UTC)
    revision = CatalogCourseRevision(component_status={"constraints": {
        "verified": True, "fresh": True, "source_status": "success",
        "source_fetched_at": now.isoformat(),
    }})
    section = CatalogSection(restrictions_status="verified",
                             restrictions_observed_at=now - timedelta(days=8),
                             restrictions_source_fetched_at=now - timedelta(days=8))
    metadata = _section_constraint_metadata(section, revision)
    assert metadata["fresh"] is False
    assert metadata["stale_reason"] == "source_fetch_expired"


@pytest.mark.parametrize("day,weekday", [("Saturday", 5), ("Sun", 6), ("Pzt", 0), ("Çar", 2), ("Cumartesi", 5), ("5", 5)])
def test_source_meeting_days_include_weekends_and_turkish_abbreviations(day, weekday):
    payload = {"course_code": "2402201", "sections": [{"section": "1", "schedule": [
        {"day": day, "time": "09:00 - 09:50"},
    ]}]}
    candidate, _, status = _parse_observation("get_course_info", {"course": "2402201"}, payload)
    assert status == "success"
    meeting = candidate["sections"][0]["meetings"][0]
    assert meeting["weekday"] == weekday
    assert meeting["status"] == "scheduled"


def test_empty_schedule_is_unknown_and_unparseable_restrictions_are_not_unrestricted():
    candidate, _, _ = _parse_observation("get_course_info", {"course": "2402201"}, {
        "course_code": "2402201", "sections": [{"section": "1", "schedule": []}],
    })
    assert candidate["sections"][0]["meetings_status"] == "unknown"
    for payload in ({"constraints": [{"given_dept": "HIST", "min_cgpa": "unknown"}]},
                    {"constraints": [{"unexpected_column": "Do not assume open"}]}):
        _, _, status = _parse_observation("get_section_constraints", {"course": "2402201", "section": "1"}, payload)
        assert status == "malformed"


def test_thesis_listing_parses_courses_and_marks_them():
    candidate, issues, status = _parse_observation("get_thesis_courses", {
        "semester": "20261", "department": "567",
    }, {
        "courses": [{
            "course_code": "5670801", "name": "SPECIAL STUDIES", "ects_credit": "10.0",
            "credit": "0.00 (4.00,2.00,)", "level": "Graduate", "type": "Thesis",
        }],
    })
    assert status == "success"
    assert issues == []
    assert candidate["courses"][0]["course_code"] == "5670801"
    assert candidate["courses"][0]["data"]["thesis"] is True
