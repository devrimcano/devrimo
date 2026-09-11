from app.planning.models import PlanState
from app.planning.solver import SolverGroup, enumerate_solutions
from app.planning.workspace import solve_plan_state


def test_shared_solver_uses_minute_ranges_for_conflicts():
    groups = [
        SolverGroup(
            key="A",
            options=(
                {"day": "Mon", "start_minute": 540, "duration_minutes": 60},
            ),
        ),
        SolverGroup(
            key="B",
            options=(
                {"day": "Mon", "start_minute": 570, "duration_minutes": 60},
            ),
        ),
    ]
    solutions = enumerate_solutions(
        groups,
        lambda item: [(item["day"], item["start_minute"], item["start_minute"] + item["duration_minutes"])],
    )

    assert all({choice.key for choice in solution} != {"A", "B"} for solution in solutions)
    assert {choice.key for choice in solutions[0]} == {"A"}


def test_timetable_solver_rejects_plan_when_a_timed_course_has_no_valid_section():
    state = PlanState(
        empty_days=["Tue"],
        pool=[
            {"code": "MATH101", "name": "Math", "credits": 3},
            {"code": "PHYS101", "name": "Physics", "credits": 3},
        ],
        sections={
            "MATH101": [
                {
                    "section": "1",
                    "eligible": False,
                    "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}],
                },
                {
                    "section": "2",
                    "eligible": True,
                    "meetings": [{"day": "Tue", "start_minute": 540, "duration_minutes": 60}],
                },
            ],
            "PHYS101": [
                {
                    "section": "1",
                    "eligible": True,
                    "meetings": [{"day": "Mon", "start_minute": 660, "duration_minutes": 110}],
                },
            ],
        },
    )

    solved = solve_plan_state(state)

    assert solved.entries == []
    assert solved.alternatives == []


def test_timetable_solver_never_returns_a_partial_conflict_free_plan():
    state = PlanState(
        pool=[
            {"code": "MATH101", "name": "Math", "credits": 3},
            {"code": "PHYS101", "name": "Physics", "credits": 3},
        ],
        sections={
            "MATH101": [{
                "section": "1",
                "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}],
            }],
            "PHYS101": [{
                "section": "1",
                "meetings": [{"day": "Mon", "start_minute": 570, "duration_minutes": 60}],
            }],
        },
    )

    solved = solve_plan_state(state)

    assert solved.entries == []
    assert solved.alternatives == []


def test_timetable_solver_can_ignore_a_course_without_published_times():
    state = PlanState(
        pool=[
            {"code": "MATH101", "name": "Math", "credits": 3},
            {"code": "PHYS101", "name": "Physics", "credits": 3},
        ],
        sections={
            "MATH101": [{"section": "1", "meetings": []}],
            "PHYS101": [{
                "section": "1",
                "meetings": [{"day": "Mon", "start_minute": 660, "duration_minutes": 110}],
            }],
        },
    )

    solved = solve_plan_state(state)

    assert {entry.code for entry in solved.entries} == {"PHYS101"}
    assert len(solved.alternatives) == 1


def test_timetable_solver_omits_a_fully_restricted_course_without_returning_a_partial_choice():
    state = PlanState(
        pool=[
            {"code": "MATH101", "name": "Math", "credits": 3},
            {"code": "HIST101", "name": "History", "credits": 3},
        ],
        sections={
            "MATH101": [{
                "section": "1",
                "eligible": True,
                "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}],
            }],
            "HIST101": [{
                "section": "1",
                "eligible": False,
                "meetings": [{"day": "Tue", "start_minute": 540, "duration_minutes": 60}],
            }],
        },
    )

    solved = solve_plan_state(state)

    assert {entry.code for entry in solved.entries} == {"MATH101"}
    assert len(solved.alternatives) == 1


def test_timetable_solver_keeps_unknown_constraint_as_visible_risk():
    state = PlanState(
        pool=[{"code": "MATH101", "name": "Math", "credits": 3}],
        sections={"MATH101": [{
            "section": "1",
            "eligible": None,
            "reason": "restriction table unavailable",
            "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}],
        }]},
    )

    solved = solve_plan_state(state)

    assert len(solved.entries) == 1
    assert solved.entries[0].tentative is True
    assert solved.entries[0].verification_status == "unverified_constraints"
    assert solved.entries[0].verification_reason == "restriction table unavailable"


def test_timetable_solver_global_override_marks_closed_section():
    state = PlanState(
        ignore_constraints=True,
        pool=[{"code": "MATH101", "name": "Math", "credits": 3}],
        sections={"MATH101": [{
            "section": "1",
            "eligible": False,
            "reason": "department restriction",
            "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}],
        }]},
    )

    solved = solve_plan_state(state)

    assert len(solved.entries) == 1
    assert solved.entries[0].verification_status == "restriction_overridden"
    assert solved.entries[0].restriction_override_scope == "global"


def test_specific_override_status_survives_legacy_round_trip():
    state = PlanState.from_legacy_payload({"entries": [{
        "id": "manual-1",
        "code": "MATH101",
        "section": "1",
        "day": "Mon",
        "start_minute": 540,
        "duration_minutes": 60,
        "tentative": True,
        "verification_status": "restriction_overridden",
        "verification_reason": "department restriction",
        "restriction_override_scope": "section",
    }]})

    entry = state.entries[0]
    assert entry.verification_status == "restriction_overridden"
    assert entry.verification_reason == "department restriction"
    assert entry.restriction_override_scope == "section"


def test_a_stale_duplicate_section_list_cannot_undo_a_verdict():
    """A plan holding the same course under two keys still respects the verdicts.

    Production plans carry both: the seven-digit key, which is where the
    eligibility verdicts are written, and a letter key left by an older client
    with no verdicts in it at all. Reading both and concatenating gave every
    blocked section an unjudged twin, so the solver placed sections the pool
    was flagging red and the restriction check looked switched off.
    """
    meetings = [{"day": "Mon", "start_minute": 540, "duration_minutes": 60}]
    state = PlanState(
        pool=[{"code": "HIST 2201", "raw_code": "2402201", "name": "History", "credits": 2}],
        sections={
            "2402201": [
                {"section": "31", "eligible": False, "reason": "Surname A-K", "meetings": meetings},
                {"section": "32", "eligible": True, "meetings": [
                    {"day": "Wed", "start_minute": 540, "duration_minutes": 60},
                ]},
            ],
            # The same two sections, as the older client stored them.
            "HIST2201": [
                {"section": "31", "meetings": meetings},
                {"section": "32", "meetings": [
                    {"day": "Wed", "start_minute": 540, "duration_minutes": 60},
                ]},
            ],
        },
    )

    solved = solve_plan_state(state)

    placed = {(entry.code, entry.section) for entry in solved.entries}
    assert placed == {("HIST 2201", "32")}
    assert all(
        entry.section != "31"
        for alternative in solved.alternatives
        for entry in alternative
    )


def test_legacy_projection_keeps_exact_meeting_minutes():
    state = PlanState.from_legacy_payload(
        {
            "courses": [
                {
                    "code": "MATH101",
                    "name": "Math",
                    "section": "1",
                    "credits": 3,
                    "meetings": [{"day": "Mon", "start": 8, "duration": 2}],
                }
            ]
        }
    )

    assert state.entries[0].start_minute == 520
    assert state.entries[0].duration_minutes == 110
    assert state.to_payload()["entries"][0]["start_minute"] == 520


def test_server_legacy_converter_owns_aliases_clamps_and_generated_entries():
    state = PlanState.from_legacy_payload(
        {
            "courses": [
                {
                    "code": "MATH101",
                    "name": "Math",
                    "section": "1",
                    "credits": 3,
                    "meetings": [{"day": "not-a-day", "startMinute": 1500, "durationMinutes": 1440}],
                }
            ],
            "pool": [{"code": "MATH101", "rawCode": "2360101"}],
            "favorites": [[{"code": "MATH101", "day": "Tue", "start": 9, "duration": 1}]],
        }
    )

    entry = state.entries[0]
    assert (entry.day, entry.start_minute, entry.duration_minutes) == ("Mon", 1439, 1)
    assert state.pool[0].raw_code == "2360101"
    assert state.favorites[0][0].start_minute == 580
