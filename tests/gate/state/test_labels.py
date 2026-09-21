"""Priority/difficulty label vocabulary (design note 11 §4)."""

from gate.state.labels import (
    Difficulty,
    Priority,
    difficulty_label,
    escalate,
    parse_difficulty,
    parse_priority,
    priority_label,
)


class TestPriorityLabels:
    def test_labels_round_trip_except_normal(self):
        # Absence = normal (note 11 §4): NORMAL is expressed by removing
        # the priority label, never by adding one.
        assert priority_label(Priority.HIGH) == "choir/priority:high"
        assert priority_label(Priority.LOW) == "choir/priority:low"
        assert priority_label(Priority.NORMAL) is None
        assert parse_priority(["choir/priority:high"]) == Priority.HIGH
        assert parse_priority(["choir/priority:low"]) == Priority.LOW
        assert parse_priority(["choir/available", "choir/type:prove"]) == Priority.NORMAL

    def test_parse_unknown_value_is_normal(self):
        # Fail-open: a mangled label never changes claimability semantics.
        assert parse_priority(["choir/priority:urgent"]) == Priority.NORMAL

    def test_priority_ordering(self):
        assert Priority.LOW < Priority.NORMAL < Priority.HIGH


class TestDifficultyLabels:
    def test_labels_round_trip(self):
        assert difficulty_label(Difficulty.EASY) == "choir/difficulty:easy"
        assert difficulty_label(Difficulty.MEDIUM) == "choir/difficulty:medium"
        assert difficulty_label(Difficulty.HARD) == "choir/difficulty:hard"
        assert parse_difficulty(["choir/difficulty:medium"]) == Difficulty.MEDIUM

    def test_absent_or_unknown_is_unrated(self):
        # None = unrated; unrated passes every filter (note 11 §4).
        assert parse_difficulty(["choir/available"]) is None
        assert parse_difficulty(["choir/difficulty:insane"]) is None


class TestEscalate:
    def test_escalation_steps_up_and_caps(self):
        assert escalate(Priority.LOW) == Priority.NORMAL
        assert escalate(Priority.NORMAL) == Priority.HIGH
        assert escalate(Priority.HIGH) == Priority.HIGH
