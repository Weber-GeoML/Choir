"""Upgrade helpers — protocol-pin writer.

Consumed by `scripts/upgrade-project.sh` (design note 13 §4) and by
`scripts/new-project.sh` at bootstrap. Pure, testable modules; no
subprocess/network side effects beyond the file they're told to write.
"""
