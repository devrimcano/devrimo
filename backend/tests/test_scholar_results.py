"""Tool results reach the model projected, not as the workspace envelope.

A real prerequisites read was 5,692 characters, of which the `_catalog` block
(release ids, components, registration window) was 1,744; other reads crossed
the 6,000-character bound and arrived as a truncated preview, which one answer
reported back as "the list came back shortened". Projection removes the
envelope and nothing else - except that the components' fresh/stale state is
kept, because an answer must not present stale data as current.
"""

import json

from app.agents.scholar.context import _selected
from app.agents.scholar.hooks import _requested_read
from app.agents.scholar.intent import context_fields, guidance
from app.agents.scholar.results import (
    EXPANDED_RESULT_CHARS,
    SECTION_PREVIEW,
    UPDATES_PREVIEW,
    bound,
    project,
    project_result,
)


def test_projection_compacts_the_catalog_envelope_and_keeps_every_domain_field():
    payload = {
        "resource": {"kind": "catalog.prerequisites", "key": "5670201"},
        "data": {
            "prerequisite_groups": [
                {
                    "group_no": 1,
                    "logic": "AND",
                    "raw_text": None,
                    "requirements": [{"course_label": "MATH 260 - BASIC LINEAR ALGEBRA", "minimum_grade": "DD"}],
                }
            ],
            "_catalog": {
                "release_id": "beec1490",
                "course_revision_id": "22856d9e",
                "components": {
                    "prerequisites": {
                        "fresh": True,
                        "verified": True,
                        "observed_at": "2026-09-11T15:39:38+00:00",
                        "source_status": "success",
                    },
                    # The read-time stale flag is the whole reason this summary
                    # exists: verified yesterday is not fresh today.
                    "sections": {"fresh": False, "verified": True, "source_status": "stale"},
                },
                "registration_window": {"start": "2026-09-14"},
            },
        },
        "provenance": {"source": "academic_catalog", "accessed_at": "t", "freshness": None},
    }
    out = project(payload)
    assert "_catalog" not in out["data"]
    assert "raw_text" not in out["data"]["prerequisite_groups"][0]
    assert "freshness" not in out["provenance"]
    assert out["data"]["freshness"]["components"] == {
        "prerequisites": {"fresh": True, "verified": True},
        "sections": {"fresh": False, "verified": True},
    }
    assert out["data"]["freshness"]["registration_window"] == {"start": "2026-09-14"}
    assert "release_id" not in out["data"]["freshness"]
    assert out["data"]["prerequisite_groups"][0]["requirements"][0] == {
        "course_label": "MATH 260 - BASIC LINEAR ALGEBRA",
        "minimum_grade": "DD",
    }
    assert out["provenance"] == {"source": "academic_catalog", "accessed_at": "t"}


def test_projection_reaches_into_nested_lists():
    value = [{"keep": 1, "_drop": 2, "list": [{"keep": None, "also": "x"}]}]
    assert project(value) == [{"keep": 1, "list": [{"also": "x"}]}]


def _sections_envelope(count: int) -> dict:
    sections = [
        {
            "section": str(index + 1),
            "section_code": str(index + 1),
            "status": "listed",
            "notes": "",
            "syllabus_url": None,
            "restrictions_observed_at": "2026-09-11T15:39:31+00:00",
            "meetings": [],
            "instructors": [{"source_name": "STAFF", "match_method": "unmatched", "resolved": False}],
            "restrictions": [
                {
                    "restriction_group": "1",
                    "row_index": 0,
                    "start_char": "AA",
                    "end_char": "ÇA",
                    "min_year": 2,
                    "max_year": 95,
                    "min_cgpa": 0.0,
                    "max_cgpa": 4.0,
                    "verified": True,
                    "status": "verified",
                }
            ],
            "section_number": str(index + 1),
        }
        for index in range(count)
    ]
    return {
        "resource": {"kind": "catalog.sections", "key": "EE 201"},
        "data": {
            "course": "5670201",
            "sections": [{"section": "stub"}],
            "data": {
                "course_code": "5670201",
                "title": "CIRCUIT THEORY I",
                "local_credits": 4.0,
                "ects": 6.0,
                "component_status": {"sections": {"fresh": True, "verified": True, "observed_at": "t"}},
                "prerequisite_groups": [{"group_no": 1, "requirements": ["x"]}],
                "sections": sections,
                "_catalog": {
                    "release_id": "r",
                    "components": {
                        "sections": {"fresh": True, "verified": True, "observed_at": "t", "source_status": "success"}
                    },
                },
            },
        },
        "provenance": {"source": "academic_catalog", "accessed_at": "t", "freshness": "cached"},
    }


def test_section_projection_unwraps_and_offers_the_rest():
    out = project_result("catalog.sections", _sections_envelope(12))
    data = out["data"]
    assert len(data["sections"]) == SECTION_PREVIEW
    assert data["sections_total"] == 12
    assert data["sections_omitted"] == 12 - SECTION_PREVIEW
    assert data["sections"][0] == {
        "section": "1",
        "instructors": ["STAFF"],
        # The full CGPA range and a 95-year ceiling restrict nothing; min_year
        # 2 does, so it stays.
        "restrictions": [{"restriction_group": "1", "start_char": "AA", "end_char": "ÇA", "min_year": 2}],
    }
    assert data["title"] == "CIRCUIT THEORY I"
    assert "component_status" not in data and "prerequisite_groups" not in data
    assert "data" not in data
    assert data["freshness"]["components"] == {"sections": {"fresh": True, "verified": True}}


def test_totals_survive_a_truncated_result_and_meetings_stay_readable():
    """A preview cut from the end must keep the number the answer declares."""
    envelope = _sections_envelope(12)
    inner = envelope["data"]["data"]
    first = inner["sections"][0]
    first["meetings"] = [
        {
            "weekday": 3,
            "start_minute": 520,
            "end_minute": 630,
            "room": "M104",
            "status": "scheduled",
            "raw_label": "08:40-10:30",
        }
    ]
    first["restrictions"] = [
        {
            "restriction_group": "1",
            "row_index": index,
            "given_department": "AEE",
            "start_char": "AA",
            "end_char": "AZ",
            "min_cgpa": 0.0,
            "max_cgpa": 4.0,
            "min_year": 0,
            "max_year": 95,
            "verified": True,
        }
        for index in range(5)
    ]
    data = project_result("catalog.sections", envelope)["data"]
    keys = list(data.keys())
    assert keys.index("sections_total") < keys.index("sections")
    section = data["sections"][0]
    assert section["meetings"] == [{"weekday": 3, "raw_label": "08:40-10:30", "room": "M104"}]
    assert len(section["restrictions"]) == 4
    assert section["restrictions_omitted"] == 1
    assert "min_cgpa" not in section["restrictions"][0]


def test_legacy_course_info_prefers_normalized_sibling_for_people_and_schedule():
    """The raw legacy row is retained for restrictions, not used over facts."""
    envelope = _sections_envelope(1)
    raw = envelope["data"]["data"]
    raw["sections"][0] = {
        "section_code": "1",
        "instructors": ["Raw Instructor"],
        "schedule": [{"day": "Monday", "time": "08:40-10:30", "room": "RAW"}],
        "restrictions": [{"restriction_group": "1", "given_department": "CENG"}],
    }
    envelope["data"]["sections"] = [
        {
            "section": "1",
            "instructor": "Normalized Instructor",
            "meetings": [
                {"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "NORM"}
            ],
            "constraint": "",
        }
    ]

    section = project_result("catalog.sections", envelope)["data"]["sections"][0]
    assert section["instructors"] == ["Normalized Instructor"]
    assert section["meetings"] == [
        {"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "NORM"}
    ]
    assert section["restrictions"] == [{"restriction_group": "1", "given_department": "CENG"}]


def test_published_course_info_keeps_typed_instructor_and_meeting_facts():
    """Published detail rows use plural typed instructors and weekday labels."""
    envelope = _sections_envelope(1)
    envelope["data"]["data"]["sections"][0] = {
        "section_code": "1",
        "instructors": [{"source_name": "Prof. Published", "position": "Dr."}],
        "meetings": [
            {
                "weekday": 2,
                "start_minute": 520,
                "end_minute": 630,
                "raw_label": "08:40-10:30",
                "room": "P1",
                "status": "scheduled",
            }
        ],
    }
    # A detail response can arrive without the legacy normalized sibling.
    envelope["data"].pop("sections")

    section = project_result("catalog.sections", envelope)["data"]["sections"][0]
    assert section["instructors"] == ["Prof. Published"]
    assert section["meetings"] == [{"weekday": 2, "raw_label": "08:40-10:30", "room": "P1"}]


def test_expanded_sections_overflow_with_structured_omission_not_json_prefix():
    """Large valid rows stay complete while the list reports what did not fit."""
    envelope = _sections_envelope(100)
    for section in envelope["data"]["data"]["sections"]:
        section["notes"] = "note " * 500
        section["instructors"] = [{"source_name": "Instructor " + "x" * 500} for _ in range(20)]
        section["meetings"] = [
            {"weekday": 1, "raw_label": "08:40-10:30 " + "x" * 500, "room": "M" * 500}
            for _ in range(20)
        ]
        section["restrictions"] = [
            {"restriction_group": str(index), "given_department": "CENG"}
            for index in range(10)
        ]

    out = project_result("catalog.sections", envelope, expand=True)
    serialized = json.dumps(out, ensure_ascii=False)
    data = out["data"]
    assert len(serialized) <= EXPANDED_RESULT_CHARS
    assert bound(out, limit=EXPANDED_RESULT_CHARS) == out
    assert "truncated" not in out
    assert data["sections_total"] == 100
    assert data["sections_omitted"] == 100 - data["sections_returned"]
    assert data["sections_omission_reason"] == "result_bound"
    assert data["sections"][0]["notes_truncated"] is True
    assert data["sections"][0]["instructors_omitted"] == 12
    assert data["sections"][0]["meetings_omitted"] == 12
    assert data["sections"][0]["restrictions_omitted"] == 9
    assert data["freshness"]["components"]["sections"] == {"fresh": True, "verified": True}


def test_sections_are_all_kept_when_the_student_asked_for_all():
    out = project_result("catalog.sections", _sections_envelope(12), expand=True)
    assert len(out["data"]["sections"]) == 12
    assert "sections_omitted" not in out["data"]


def test_expanded_sixty_one_section_course_fits_without_arbitrary_truncation():
    out = project_result("catalog.sections", _sections_envelope(61), expand=True)
    assert len(json.dumps(out, ensure_ascii=False)) <= EXPANDED_RESULT_CHARS
    assert len(out["data"]["sections"]) == 61
    assert "sections_omitted" not in out["data"]


def test_expand_keeps_a_giant_list_lean():
    envelope = _sections_envelope(30)
    for section in envelope["data"]["data"]["sections"]:
        section["restrictions"] = section["restrictions"] * 3
    out = project_result("catalog.sections", envelope, expand=True)
    sections = out["data"]["sections"]
    assert len(sections) == 30
    assert len(sections[0]["restrictions"]) == 1
    assert sections[0]["restrictions_omitted"] == 2


class _Context:
    def __init__(self, dependencies):
        self.dependencies = dependencies


def test_expand_is_only_honored_when_the_student_allowed_it():
    arguments = {"resource": {"kind": "catalog.sections", "expand": True}}
    assert _requested_read(arguments, _Context({"expand_allowed": True})) == ("catalog.sections", True)
    assert _requested_read(arguments, _Context({"expand_allowed": False})) == ("catalog.sections", False)
    assert _requested_read(arguments, _Context({})) == ("catalog.sections", False)
    unrequested = {"resource": {"kind": "catalog.sections"}}
    assert _requested_read(unrequested, _Context({"expand_allowed": True})) == ("catalog.sections", False)


def test_projection_does_not_mutate_the_envelope():
    """A preview and an expand project the same input independently."""
    envelope = _sections_envelope(12)
    preview = project_result("catalog.sections", envelope)
    full = project_result("catalog.sections", envelope, expand=True)
    assert len(preview["data"]["sections"]) == SECTION_PREVIEW
    assert len(full["data"]["sections"]) == 12


def _updates_envelope(count: int) -> dict:
    return {
        "resource": {"kind": "my.updates"},
        "data": {
            "mode": "feed",
            "items": [
                {
                    "id": f"u{index}",
                    "document_id": "d" * 64,
                    "type": "announcement" if index % 2 else "event",
                    "title": f"Item {index}",
                    "summary": None,
                    "content": "x" * 1000,
                    "url": "https://example",
                    "source_url": "https://example",
                    "score": 0.5,
                    "origin": "campus",
                    "read": False,
                    "published_at": "2026-09-10T00:00:00+00:00",
                }
                for index in range(count)
            ],
            "personalized_by": {"department": "CENG"},
        },
        "provenance": {"source": "devrimo", "accessed_at": "t", "freshness": "cached"},
    }


def test_updates_projection_keeps_what_an_answer_uses():
    out = project_result("my.updates", _updates_envelope(30))
    data = out["data"]
    assert 0 < len(data["items"]) <= UPDATES_PREVIEW
    assert data["items_total"] == 30
    assert data["items_omitted"] == 30 - len(data["items"])
    assert data["items_omission_reason"] in {"preview", "result_bound"}
    first = data["items"][0]
    assert first["title"] == "Item 0"
    assert first["type"] == "event"
    assert first["when"] == "2026-09-10T00:00:00+00:00"
    assert len(first["summary"]) == 241
    assert "document_id" not in first and "content" not in first and "score" not in first
    keys = list(data.keys())
    assert keys.index("items_total") < keys.index("items")


def test_updates_are_all_kept_when_asked_for_all():
    out = project_result("my.updates", _updates_envelope(30), expand=True)
    assert len(out["data"]["items"]) == 30
    assert "items_omitted" not in out["data"]


def test_oversized_updates_stay_structural_and_report_omissions():
    envelope = _updates_envelope(100)
    for item in envelope["data"]["items"]:
        item["title"] = "T" * 10_000
        item["summary"] = "S" * 100_000
        item["source"] = "source-" + "x" * 10_000
        item["url"] = "https://example/" + "u" * 10_000

    out = project_result("my.updates", envelope, expand=True)
    serialized = json.dumps(out, ensure_ascii=False)
    data = out["data"]
    assert len(serialized) <= EXPANDED_RESULT_CHARS
    assert bound(out, limit=EXPANDED_RESULT_CHARS) == out
    assert "truncated" not in out
    assert data["items_total"] == 100
    assert data["items_omitted"] == 100 - data["items_returned"]
    assert data["items_omission_reason"] == "result_bound"
    assert set(data["items"][0]["fields_truncated"]) == {"title", "summary", "source", "url"}


def test_the_guidance_tells_the_model_to_offer_the_rest():
    assert "sections_omitted" in guidance("sections")
    assert "resource.expand" in guidance("sections")
    assert "my.updates" in guidance("announcements")
    assert "resource.expand" in guidance("announcements")


def test_selection_keeps_the_diet_fields_whatever_the_intent():
    payload = {
        "display_name": "A",
        "planned_timetable": {"term": "20261"},
        "answer_guidance": "shape",
        "current_focus": {"courses": ["EE 201"]},
        "intent": "credits",
        "requested_scope": {"term": "20252", "section": None},
        "expand_allowed": True,
    }
    kept = _selected(payload, context_fields("knowledge"))
    for key in ("display_name", "answer_guidance", "current_focus", "intent", "requested_scope", "expand_allowed"):
        assert key in kept
    assert kept["expand_allowed"] is True
    assert "planned_timetable" not in kept
