"""Tests for `viz.render`'s reading of the plan out of a timeline."""

from __future__ import annotations

from viz.model import Node
from viz.render import in_plan


def test_a_published_task_is_in_the_plan() -> None:
    """A worker's decomposition needs no filing: the children are published,
    and a task is the project saying it means to prove the thing."""
    assert in_plan(Node(decl="Proj.child", issue=42))


def test_a_node_filed_under_a_group_is_in_the_plan() -> None:
    assert in_plan(Node(decl="Proj.main", group="core"))


def test_a_helper_is_not_in_the_plan_for_having_dependencies() -> None:
    """Once a plan records the edges its terms really have, almost every
    declaration has one; counting that leaves neither scope answering its
    own question."""
    assert not in_plan(Node(decl="Proj.helper", declares_deps=True))


def test_a_bare_declaration_is_not_in_the_plan() -> None:
    assert not in_plan(Node(decl="Proj.leaf"))
