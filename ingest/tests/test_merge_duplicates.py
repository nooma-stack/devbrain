"""merge_duplicates.plan_merges grouping rules (no DB)."""

from datetime import datetime, timedelta

from merge_duplicates import Node, plan_merges

T0 = datetime(2026, 8, 1)


def node(i, *, edges=False, hits=0, length=100, age_days=0):
    return Node(i, "decision", "devbrain", f"title {i}", edges, hits, length, T0 + timedelta(days=age_days))


def nodes(*ns):
    return {n.id: n for n in ns}


def test_keeps_one_copy_and_prefers_hits_then_length_then_age():
    ns = nodes(node("a", age_days=2), node("b", hits=3), node("c", length=400))
    pairs = [("a", "b", 0.97, "same session"), ("b", "c", 0.96, "same session"), ("a", "c", 0.98, "same session")]

    merges = plan_merges(pairs, ns)

    assert {m.keep for m in merges} == {"b"}
    assert {m.drop for m in merges} == {"a", "c"}


def test_no_chaining_through_a_middle_row():
    # a~b and b~c but a and c are not similar: keeping a merges only b; c stays.
    ns = nodes(node("a", hits=5), node("b"), node("c"))
    pairs = [("a", "b", 0.96, "same session"), ("b", "c", 0.96, "same session")]

    merges = plan_merges(pairs, ns)

    assert [(m.keep, m.drop) for m in merges] == [("a", "b")]


def test_rows_with_edges_are_never_archived_but_can_be_kept():
    ns = nodes(node("linked", edges=True), node("plain", hits=9), node("other"))
    pairs = [("linked", "plain", 0.99, "same title"), ("plain", "other", 0.99, "same title")]

    merges = plan_merges(pairs, ns)

    assert ("linked", "plain") in [(m.keep, m.drop) for m in merges]
    assert all(m.drop != "linked" for m in merges)


def test_two_linked_rows_both_stay():
    ns = nodes(node("x", edges=True), node("y", edges=True))
    assert plan_merges([("x", "y", 0.99, "same title")], ns) == []


def test_highest_similarity_reason_wins_for_a_pair():
    ns = nodes(node("a", hits=1), node("b"))
    pairs = [("a", "b", 0.95, "same session"), ("a", "b", 0.99, "same title")]

    [m] = plan_merges(pairs, ns)

    assert (m.sim, m.reason) == (0.99, "same title")
