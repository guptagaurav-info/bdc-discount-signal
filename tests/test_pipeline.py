import numpy as np
import pandas as pd

from bdcsignal.pipeline import bucket, build_panel, nav_breaks, price_on_or_after, split_adjust

CFG = {"nav_lag_days": 50, "horizon_quarters": 4, "pnav_bounds": [0.3, 2.0], "max_qoq_nav_jump": 0.25}


def _toy(nav_path=(10, 10, 9.8, 9.6, 9.4, 9.0, 8.8, 8.6, 8.4)):
    qs = pd.date_range("2022-03-31", periods=len(nav_path), freq="QE")
    nav = pd.DataFrame({"symbol": "AAA", "q": qs, "nav_ps": nav_path})
    d = pd.bdate_range("2021-06-01", "2025-12-31")
    px = pd.DataFrame({"symbol": "AAA", "d": d, "close": 9.0})
    dv = pd.DataFrame({"symbol": "AAA", "d": d[::63], "amount": 0.30})
    return nav, px, dv


def test_price_is_taken_only_after_the_nav_is_public():
    nav, px, dv = _toy()
    P = build_panel(CFG, nav, px, dv)
    assert ((P.avail - P.q).dt.days >= CFG["nav_lag_days"]).all()


def test_future_prices_and_navs_do_not_change_todays_signal():
    nav, px, dv = _toy(); P = build_panel(CFG, nav, px, dv)
    px2 = px.copy(); first = P.avail.iloc[0]
    px2.loc[px2.d > first, "close"] = 1.0                       # a crash after the observation date
    nav2 = nav.copy(); nav2.loc[nav2.q > P.q.iloc[0], "nav_ps"] *= 0.9
    P2 = build_panel(CFG, nav2, px2, dv)
    assert np.isclose(P.pnav.iloc[0], P2.pnav.iloc[0])


def test_forward_nav_change_is_four_quarters_ahead():
    nav, px, dv = _toy(); P = build_panel(CFG, nav, px, dv)
    r = P.iloc[0]; assert np.isclose(r.nav_chg_fwd, 9.4 / 10 - 1)


def test_no_price_before_listing():
    p = pd.DataFrame({"d": pd.bdate_range("2024-01-10", periods=10), "close": 1.0})
    assert price_on_or_after(p, pd.Timestamp("2023-06-01"))[0] is None
    assert price_on_or_after(p, pd.Timestamp("2024-01-09"))[0] == pd.Timestamp("2024-01-10")


def test_split_adjust_restates_shares_before_the_split():
    nav = pd.DataFrame({"symbol": ["A", "A"], "q": pd.to_datetime(["2023-12-31", "2024-06-30"]),
                        "common_stock_equity": [100.0, 100.0], "ordinary_shares_number": [20.0, 10.0]})
    sp = pd.DataFrame({"symbol": ["A"], "d": [pd.Timestamp("2024-03-01")], "split_factor": ["1:2"]})
    out = split_adjust(nav, sp)
    assert np.allclose(out.nav_ps, [10.0, 10.0])


def test_nav_break_screen_drops_implausible_jumps():
    nav, px, dv = _toy((10, 10, 20, 20, 20, 20, 20, 20, 20))
    assert len(nav_breaks(nav, 0.25)) == 1
    P = build_panel(CFG, nav, px, dv)
    assert not ((P.q >= pd.Timestamp("2022-03-31")) & (P.q <= pd.Timestamp("2022-09-30"))).any()
    assert len(P.attrs["dropped"]) > 0


def test_buckets():
    assert list(bucket(np.array([0.7, 0.85, 0.99, 1.0, 1.3]), [0.85, 1.0])) == ["deep", "modest", "modest", "premium", "premium"]
