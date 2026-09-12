"""Tool results reach the model projected, not as the workspace envelope.

A real prerequisites read was 5,692 characters, of which the `_catalog` block
(release ids, components, registration window) was 1,744; other reads crossed
the 6,000-character bound and arrived as a truncated preview, which one answer
reported back as "the list came back shortened". Projection removes the
envelope and nothing else - except that the components' fresh/stale state is
kept, because an answer must not present stale data as current.
"""

from app.agents.scholar.results import project
from app.agents.scholar.context import _selected
from app.agents.scholar.intent import context_fields


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
    assert out["data"]["prerequisite_groups"][0]["requirements"][0] == {
        "course_label": "MATH 260 - BASIC LINEAR ALGEBRA",
        "minimum_grade": "DD",
    }
    assert out["provenance"] == {"source": "academic_catalog", "accessed_at": "t"}


def test_projection_reaches_into_nested_lists():
    value = [{"keep": 1, "_drop": 2, "list": [{"keep": None, "also": "x"}]}]
    assert project(value) == [{"keep": 1, "list": [{"also": "x"}]}]


def test_selection_keeps_the_diet_fields_whatever_the_intent():
    payload = {
        "display_name": "A",
        "planned_timetable": {"term": "20261"},
        "answer_guidance": "shape",
        "current_focus": {"courses": ["EE 201"]},
        "intent": "credits",
        "requested_scope": {"term": "20252", "section": None},
    }
    kept = _selected(payload, context_fields("knowledge"))
    for key in ("display_name", "answer_guidance", "current_focus", "intent", "requested_scope"):
        assert key in kept
    assert "planned_timetable" not in kept
