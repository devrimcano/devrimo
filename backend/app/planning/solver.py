"""The bounded, minute precise section search shared by every planner client."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
Meeting = tuple[str, int, int]


@dataclass(frozen=True)
class SolverGroup[T]:
    """One course (or other schedulable item) and its available sections."""

    key: str
    options: tuple[T, ...]


@dataclass(frozen=True)
class SolverChoice[T]:
    key: str
    option: T


def _conflicts(candidate: Iterable[Meeting], selected: Iterable[Meeting]) -> bool:
    for day, start, end in candidate:
        for other_day, other_start, other_end in selected:
            if day == other_day and start < other_end and other_start < end:
                return True
    return False


def enumerate_solutions[T](
    groups: Iterable[SolverGroup[T]],
    meetings: Callable[[T], Iterable[Meeting]],
    *,
    avoid_conflicts: bool = True,
    max_solutions: int = 200,
    max_nodes: int = 150_000,
    allow_skip: bool | Callable[[SolverGroup[T]], bool] = True,
    allow_option: Callable[[tuple[SolverChoice[T], ...], T], bool] | None = None,
    initial_meetings: Iterable[Meeting] = (),
) -> list[list[SolverChoice[T]]]:
    """Enumerate deterministic bounded combinations using exact minute ranges.

    The caller owns eligibility and ranking. This helper owns the section
    combination search and overlap rule, which keeps the browser timetable and
    the assistant's semester planner on the same implementation.
    """

    ordered = sorted(groups, key=lambda group: (len(group.options), group.key))
    solutions: list[list[SolverChoice[T]]] = []
    selected: list[SolverChoice[T]] = []
    selected_meetings: list[Meeting] = list(initial_meetings)
    nodes = 0

    def may_skip(group: SolverGroup[T]) -> bool:
        return allow_skip(group) if callable(allow_skip) else allow_skip

    def visit(index: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or len(solutions) >= max_solutions:
            return
        if index == len(ordered):
            solutions.append(list(selected))
            return
        group = ordered[index]
        for option in group.options:
            if allow_option is not None and not allow_option(tuple(selected), option):
                continue
            option_meetings = tuple(meetings(option))
            if avoid_conflicts and _conflicts(option_meetings, selected_meetings):
                continue
            selected.append(SolverChoice(group.key, option))
            selected_meetings.extend(option_meetings)
            visit(index + 1)
            if option_meetings:
                del selected_meetings[-len(option_meetings) :]
            selected.pop()
            if nodes > max_nodes or len(solutions) >= max_solutions:
                return
        if may_skip(group):
            visit(index + 1)

    visit(0)
    return solutions
