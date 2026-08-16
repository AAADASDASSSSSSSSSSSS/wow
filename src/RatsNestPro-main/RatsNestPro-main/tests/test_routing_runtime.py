from ratsnestpro.eda.routing import _router_timeout, pass_budget


def test_multilayer_routing_gets_a_larger_default_budget(monkeypatch) -> None:
    monkeypatch.delenv("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", raising=False)

    assert _router_timeout(2) == 1800
    assert _router_timeout(4) == 3600


def test_routing_timeout_override_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", "30")
    assert _router_timeout(4) == 300

    monkeypatch.setenv("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", "99999")
    assert _router_timeout(2) == 7200


def test_pass_budget_scales_with_connectivity_and_is_bounded() -> None:
    small = {"SIG": [["U1", "1"], ["U2", "1"]]}
    dense = {
        f"N{index}": [[f"U{index}", str(pin)] for pin in range(1, 14)]
        for index in range(80)
    }

    assert pass_budget(small, 2) == 20
    assert pass_budget(small, 4) == 26
    assert pass_budget(dense, 4) == 100
