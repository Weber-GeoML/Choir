from gate.state.reduction_record import (
    ReductionParseError,
    ReductionRecord,
    parse_reduction_block,
)

VALID = """\
Proves the target modulo two obligations.

```choir-reduction
choir-reduction-version: 1
parent: PMC.sidorenko_tree
children:
  - decl: PMC.homSet_entropy_le
    blueprint_ref: sidorenko_tree_entropy
    note: the marginals are degree-biased
  - decl: PMC.sum_degree_mul_log_ge
```

Ready for review.
"""


def test_absent_block_is_none() -> None:
    assert parse_reduction_block("just an ordinary PR body") is None


def test_valid_block_parses() -> None:
    record = parse_reduction_block(VALID)
    assert isinstance(record, ReductionRecord)
    assert record.parent == "PMC.sidorenko_tree"
    assert [c.decl for c in record.children] == [
        "PMC.homSet_entropy_le",
        "PMC.sum_degree_mul_log_ge",
    ]
    assert record.children[0].blueprint_ref == "sidorenko_tree_entropy"
    assert record.children[1].note is None


def test_unknown_version_is_error() -> None:
    body = "```choir-reduction\nchoir-reduction-version: 2\nparent: A\nchildren:\n  - decl: B\n```"
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_child_without_decl_is_error() -> None:
    body = "```choir-reduction\nchoir-reduction-version: 1\nparent: A\nchildren:\n  - note: hi\n```"
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_empty_children_is_error() -> None:
    body = "```choir-reduction\nchoir-reduction-version: 1\nparent: A\nchildren: []\n```"
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_extra_field_is_error() -> None:
    body = (
        "```choir-reduction\nchoir-reduction-version: 1\nparent: A\n"
        "children:\n  - decl: B\nextra: nope\n```"
    )
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_malformed_yaml_is_error() -> None:
    body = "```choir-reduction\nparent: [unclosed\n```"
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_unterminated_block_is_error() -> None:
    body = "```choir-reduction\nchoir-reduction-version: 1\nparent: A\n"
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_decl_shape_is_validated() -> None:
    body = (
        "```choir-reduction\nchoir-reduction-version: 1\nparent: A\n"
        "children:\n  - decl: 'not a name'\n```"
    )
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_trailing_dot_parent_is_rejected() -> None:
    # A trailing dot splits to an empty leaf downstream; reject it here
    # rather than let a degenerate name reach the comparator.
    body = (
        "```choir-reduction\nchoir-reduction-version: 1\n"
        "parent: Project.main_bound.\nchildren:\n  - decl: B\n```"
    )
    assert isinstance(parse_reduction_block(body), ReductionParseError)


def test_unicode_parent_and_child_are_accepted() -> None:
    # Lean identifiers routinely use non-ASCII letters; a project using
    # them must still be able to declare a reduction.
    body = (
        "```choir-reduction\nchoir-reduction-version: 1\n"
        "parent: NS.α_β\nchildren:\n  - decl: NS.γ'\n```"
    )
    record = parse_reduction_block(body)
    assert isinstance(record, ReductionRecord)
    assert record.parent == "NS.α_β"
    assert record.children[0].decl == "NS.γ'"


def test_block_scalar_with_inner_fence_and_second_child() -> None:
    # Regression test: YAML block scalars can contain indented backtick lines.
    # The closing fence must match the opener's indentation to avoid truncation.
    body = """\
Some preamble.

```choir-reduction
choir-reduction-version: 1
parent: X
children:
  - decl: A
    note: |
      some text
        ```
      more text after the inner fence
  - decl: B
```

After the block.
"""
    record = parse_reduction_block(body)
    assert isinstance(record, ReductionRecord)
    assert len(record.children) == 2
    assert record.children[0].decl == "A"
    assert record.children[1].decl == "B"
    assert "more text after the inner fence" in record.children[0].note


def test_uniformly_indented_block_parses() -> None:
    # The indentation constraint must not break blocks that are uniformly indented
    # (e.g., inside a list item).
    body = """\
  ```choir-reduction
  choir-reduction-version: 1
  parent: Y
  children:
    - decl: C
  ```
"""
    record = parse_reduction_block(body)
    assert isinstance(record, ReductionRecord)
    assert record.parent == "Y"
    assert record.children[0].decl == "C"
