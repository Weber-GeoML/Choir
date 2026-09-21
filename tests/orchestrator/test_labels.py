"""Priority/difficulty label writers (design note 11 §4).

Split out of the now-deleted `tests/orchestrator/test_review_flow.py`
(spec D1) alongside `orchestrator/labels.py` — these tests cover
non-review plumbing that happened to live in the same module.
"""

from __future__ import annotations

import orchestrator.labels as labels_mod
from gate.state.labels import Priority


class TestBumpPriority:
    def test_normal_bumps_to_high(self, monkeypatch):
        ops: list[tuple] = []
        monkeypatch.setattr(labels_mod, "_edit_labels", lambda *a: ops.append(a))

        new = labels_mod.bump_priority("acme/proofs", 7, ["choir/available"])
        assert new == Priority.HIGH
        # one edit: add choir/priority:high (nothing to remove)
        assert ops == [("acme/proofs", 7, ["choir/priority:high"], [])]

    def test_high_is_a_noop(self, monkeypatch):
        ops: list[tuple] = []
        monkeypatch.setattr(labels_mod, "_edit_labels", lambda *a: ops.append(a))

        new = labels_mod.bump_priority("acme/proofs", 7, ["choir/priority:high"])
        assert new == Priority.HIGH
        assert ops == []
