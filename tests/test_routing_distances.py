"""Bay ranking uses real geometry, never name order."""
import pytest

from app import routing


@pytest.fixture(autouse=True)
def clean():
    routing.reset()
    yield
    routing.reset()


def test_precomputed_driving_distance_wins():
    routing.set_distance_table({"ENTRY1": {"S1": 900.0, "S10": 120.0}})
    assert routing.find_best_spot("ENTRY1", ["S1", "S10"]) == "S10"


def test_without_a_table_coordinates_decide_not_the_name_order():
    # The ring ranked by sorted name, so "S1" beat "S10" regardless of where
    # they physically are. Coordinates must decide instead.
    routing.set_coordinates({"ENTRY1": (0.0, 0.0), "S1": (900.0, 0.0), "S10": (100.0, 0.0)})
    assert routing.find_best_spot("ENTRY1", ["S1", "S10"]) == "S10"


def test_unknown_names_do_not_crash_and_rank_last():
    routing.set_coordinates({"ENTRY1": (0.0, 0.0), "S1": (100.0, 0.0)})
    assert routing.find_best_spot("ENTRY1", ["S1", "MYSTERY"]) == "S1"


def test_no_geometry_at_all_returns_a_stable_choice():
    assert routing.find_best_spot("ENTRY1", ["S3", "S1"]) == "S1"


def test_empty_candidate_list_returns_none():
    assert routing.find_best_spot("ENTRY1", []) is None


def test_rank_spots_is_ascending_by_distance():
    routing.set_distance_table({"ENTRY1": {"A": 30.0, "B": 10.0, "C": 20.0}})
    assert [name for name, _ in routing.rank_spots("ENTRY1", ["A", "B", "C"])] == ["B", "C", "A"]


def test_the_synthetic_ring_is_gone():
    for dead in ("circular_delta", "circular_distance", "StationRing", "ring"):
        assert not hasattr(routing, dead), f"{dead} still exists"
