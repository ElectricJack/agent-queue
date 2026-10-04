"""The repair ladder inherits history; only new commits belong to a stage."""

import pytest

from src.integration.repair_lineage import introduced_repair_commits


@pytest.mark.parametrize("prior,current,expected", [
    ([], [], []), (["a" * 40], ["a" * 40], []),
    (["a" * 40], ["a" * 40, "b" * 40], ["b" * 40]),
])
def test_introduced_repair_commits_excludes_inherited_history(prior, current, expected):
    assert introduced_repair_commits(
        {"dossier": {"repair_commits": current}}, {"dossier": {"repair_commits": prior}},
    ) == expected


@pytest.mark.parametrize("prior,current", [
    (["a" * 40], []), ([], ["a" * 40, "a" * 40]), ([], ["HEAD"]),
    ([], [{}]), ([], "a" * 40),
])
def test_introduced_repair_commits_refuses_ambiguous_lineage(prior, current):
    with pytest.raises(ValueError):
        introduced_repair_commits(
            {"dossier": {"repair_commits": current}}, {"dossier": {"repair_commits": prior}},
        )
