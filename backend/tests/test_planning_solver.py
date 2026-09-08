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


def test_timetable_solver_skips_ineligible_and_empty_day_sections():
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

    assert {entry.code for entry in solved.entries} == {"PHYS101"}
    assert solved.alternatives
    assert all(entry.code != "MATH101" for entry in solved.entries)
    assert all(entry.day != "Tue" for alternative in solved.alternatives for entry in alternative)


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
