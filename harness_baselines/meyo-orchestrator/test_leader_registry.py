import leader_registry as leaders


def test_omnigent_supervised_is_default():
    selected = leaders.select_default()
    assert selected["key"] == "omnigent"
    assert selected["score"] == 11


def test_every_leader_has_explicit_role_and_capability_matrix():
    rows = leaders.list_leaders()
    assert {row["key"] for row in rows} == {"omnigent", "polly", "autoloop", "cascade"}
    assert all(row["role"] and len(row["capabilities"]) == 11 for row in rows)


def test_workers_are_not_implicitly_promoted_to_leaders():
    assert "cc" not in leaders.CANDIDATES
    assert "codex" not in leaders.CANDIDATES
    assert "hermes" not in leaders.CANDIDATES
