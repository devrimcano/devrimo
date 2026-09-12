"""Tool results reach the model projected, not as the workspace envelope.

A real prerequisites read was 5,692 characters, of which the `_catalog` block
(release ids, components, registration window) was 1,744 and the answer used
none of it; other reads crossed the 6,000-character bound and arrived as a
truncated preview, which one answer reported back as "the list came back
shortened". Projection has to remove the envelope and nothing else.
"""

from app.agents.scholar.results import project
from app.agents.scholar.context import _selected
from app.agents.scholar.intent import context_fields


def test_projection_drops_the_envelope_and_keeps_every_domain_field():
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
            "_catalog": {"release_id": "beec1490", "components": {"sais": True}},
        },
        "provenance": {"source": "academic_catalog", "accessed_at": "t", "freshness": None},
    }
    out = project(payload)
    assert "_catalog" not in out["data"]
    assert "raw_text" not in out["data"]["prerequisite_groups"][0]
    assert "freshness" not in out["provenance"]
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
        "prefetched": [{"kind": "catalog.sections"}],
    }
    kept = _selected(payload, context_fields("knowledge"))
    for key in ("display_name", "answer_guidance", "current_focus", "intent", "prefetched"):
        assert key in kept
    assert "planned_timetable" not in kept
