from app import layout


def _names(level):
    return [s["name"] for s in layout.load_geometry(level)["spots"]]


def test_each_level_detects_itself():
    for level in ("lvl1", "lvl2", "lvl3"):
        assert layout.detect_level(_names(level)) == level


def test_unknown_names_detect_nothing():
    assert layout.detect_level(["X1", "X2"]) is None
    assert layout.detect_level([]) is None


def test_geometry_shape():
    lv2 = layout.load_geometry("lvl2")
    assert lv2["level"] == "lvl2"
    assert len([s for s in lv2["spots"] if s["purpose"] == "Park"]) == 90
    assert {s["car_type"] for s in lv2["spots"]} >= {"Any", "Electric", "Accessible"}
    assert len(lv2["fans"]) == 12 and len(lv2["zones"]) == 3
    assert all(z["type"] == "Closed" for z in lv2["zones"])
    assert lv2["bounds"]["max_x"] > lv2["bounds"]["min_x"]


def test_level3_gate_names_are_unique_so_every_gate_is_controllable():
    gates = layout.load_geometry("lvl3")["gates"]
    names = [gate["name"] for gate in gates]

    assert len(gates) == 20
    assert len(set(names)) == 20
    assert {"gate7", "gate20"} <= set(names)


def test_zone4_right_edge_has_two_exit_sensors():
    spots = {spot["name"]: spot for spot in layout.load_geometry("lvl3")["spots"]}

    assert spots["Exit103"]["purpose"] == "ExitSpot"
    assert spots["Exit104"]["purpose"] == "ExitSpot"
    assert "Entry104" not in spots


def test_missing_level_is_empty_not_an_error():
    geo = layout.load_geometry("lvl99")
    assert geo["level"] is None and geo["spots"] == [] and geo["bounds"] is None


def test_legacy_dict_shape_kept_for_split_dashboard():
    legacy = layout.load_layout("lvl1")
    assert "S1" in legacy["spots"] and "x" in legacy["spots"]["S1"]
