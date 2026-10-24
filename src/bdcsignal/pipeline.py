"""Does the public market see private-credit losses before the marks do?

Listed business development companies (BDCs) report a net asset value (NAV) per share each quarter, built
from manager marks on private loans. Their shares trade every day. This pipeline asks whether the gap
between price and NAV, observed only once the NAV is public, predicts the next year's change in NAV.
"""
import json
from pathlib import Path

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from matplotlib.ticker import MultipleLocator, PercentFormatter
from scipy import stats as st

NAVY, GOLD, GREY, RED, BLUE = "#1F3A5F", "#9A7F2E", "#8A8880", "#B5473A", "#3A6A9A"


def load(cfg):
    raw = Path(cfg["raw_dir"]); c = duckdb.connect()
    prof = c.sql(f"select symbol from '{raw}/stock_profile.parquet' where long_business_summary ilike '%business development company%'").df()
    syms = sorted(set(prof.symbol) - set(cfg["exclude"]))
    L = ",".join(repr(s) for s in syms)
    nav = c.sql(f"""select symbol, report_date, item_name, item_value::double v from '{raw}/stock_statement.parquet'
        where period_type='quarterly' and finance_type='balance_sheet' and symbol in ({L})
        and item_name in ('common_stock_equity','ordinary_shares_number')""").df()
    nav = nav.pivot_table(index=["symbol", "report_date"], columns="item_name", values="v").dropna().reset_index()
    nav["q"] = pd.to_datetime(nav.report_date)
    px = c.sql(f"select symbol, report_date, close::double as close from '{raw}/prices.parquet' where symbol in ({L}) and report_date>='2021-06-01'").df()
    px["d"] = pd.to_datetime(px.report_date)
    dv = c.sql(f"select symbol, report_date, amount::double amount from '{raw}/stock_dividend_events.parquet' where symbol in ({L})").df()
    dv["d"] = pd.to_datetime(dv.report_date)
    sp = c.sql(f"select symbol, report_date, split_factor from '{raw}/stock_split_events.parquet' where symbol in ({L})").df()
    sp["d"] = pd.to_datetime(sp.report_date)
    ty = c.sql(f"select report_date, bc_3month::double y3m from '{raw}/daily_treasury_yield.parquet'").df()
    ty["d"] = pd.to_datetime(ty.report_date)
    return syms, nav, px, dv, sp, ty


def split_adjust(nav, sp):
    """Prices are split-adjusted; restate historical share counts on the same basis."""
    nav = nav.copy(); nav["shares_adj"] = nav.ordinary_shares_number
    for _, s in sp.iterrows():
        a, b = (float(x) for x in s.split_factor.split(":"))
        m = (nav.symbol == s.symbol) & (nav.q < s.d)
        nav.loc[m, "shares_adj"] *= a / b
    nav["nav_ps"] = nav.common_stock_equity / nav.shares_adj
    return nav


def price_on_or_after(px_sym, when, max_gap_days=7):
    """First close on or after `when`; none if the stock was not trading then (e.g. before its listing)."""
    s = px_sym[px_sym.d >= when]
    if not len(s) or (s.d.iloc[0] - when).days > max_gap_days:
        return None, np.nan
    return s.d.iloc[0], s.close.iloc[0]


def nav_breaks(g, max_jump):
    """Quarter dates where NAV per share jumps implausibly (share-count or split mismatch in the source data)."""
    g = g.sort_values("q"); ch = g.nav_ps.pct_change().abs()
    return list(g.q[ch > max_jump])


def build_panel(cfg, nav, px, dv):
    lag, H = pd.Timedelta(days=cfg["nav_lag_days"]), cfg["horizon_quarters"]
    rows, dropped = [], []
    for sym, g in nav.sort_values("q").groupby("symbol"):
        p = px[px.symbol == sym].sort_values("d"); dvs = dv[dv.symbol == sym]
        if len(p) < 250: continue
        g = g.reset_index(drop=True); brk = nav_breaks(g, cfg["max_qoq_nav_jump"])
        for i, r in g.iterrows():
            # data-quality screen: skip any observation whose trailing or forward year spans a NAV-per-share break
            if any(r.q - pd.DateOffset(months=12, days=5) < b <= r.q + pd.DateOffset(months=3 * H, days=5) for b in brk):
                dropped.append(f"{sym} {r.q.date()}"); continue
            d0, p0 = price_on_or_after(p, r.q + lag)
            if d0 is None: continue
            rec = dict(symbol=sym, q=r.q, avail=d0, nav=r.nav_ps, price=p0, pnav=p0 / r.nav_ps)
            prev = g[(g.q >= r.q - pd.DateOffset(months=12, days=5)) & (g.q <= r.q - pd.DateOffset(months=12) + pd.Timedelta(days=5))]
            rec["nav_chg_trailing"] = r.nav_ps / prev.nav_ps.iloc[0] - 1 if len(prev) else np.nan
            fut = g[(g.q >= r.q + pd.DateOffset(months=3 * H) - pd.Timedelta(days=5)) & (g.q <= r.q + pd.DateOffset(months=3 * H) + pd.Timedelta(days=5))]
            rec["nav_chg_fwd"] = fut.nav_ps.iloc[0] / r.nav_ps - 1 if len(fut) else np.nan
            # 12-month total return from the date the NAV became public
            d1, p1 = price_on_or_after(p, d0 + pd.Timedelta(days=365))
            if d1 is not None and (d1 - d0).days < 380:
                div = dvs[(dvs.d > d0) & (dvs.d <= d1)].amount.sum()
                rec["tr_fwd"] = (p1 + div) / p0 - 1
            else:
                rec["tr_fwd"] = np.nan
            # trailing 12-month dividend yield at the observation date
            rec["div_yield"] = dvs[(dvs.d > d0 - pd.Timedelta(days=365)) & (dvs.d <= d0)].amount.sum() / p0
            rows.append(rec)
    P = pd.DataFrame(rows)
    lo, hi = cfg["pnav_bounds"]
    P = P[(P.pnav > lo) & (P.pnav < hi)].reset_index(drop=True)
    P.attrs["dropped"] = dropped
    return P


def bucket(x, b):
    return np.where(x < b[0], "deep", np.where(x < b[1], "modest", "premium"))


def analyse(cfg, P, ty):
    E = P.dropna(subset=["nav_chg_fwd"]).copy()
    E["bucket"] = bucket(E.pnav, cfg["buckets"])
    out = {"n_obs": int(len(E)), "n_bdcs": int(E.symbol.nunique()), "quarters": sorted(E.q.dt.strftime("%Y-%m-%d").unique().tolist())}
    rho, pv = st.spearmanr(E.pnav, E.nav_chg_fwd); out["spearman_pooled"] = [float(rho), float(pv)]
    per_q = E.groupby("q").apply(lambda g: st.spearmanr(g.pnav, g.nav_chg_fwd)[0] if len(g) >= 8 else np.nan).dropna()
    out["spearman_by_quarter"] = {k.strftime("%Y-%m-%d"): float(v) for k, v in per_q.items()}
    out["spearman_by_quarter_mean"] = float(per_q.mean()); out["spearman_positive_quarters"] = [int((per_q > 0).sum()), int(len(per_q))]
    out["spearman_t"] = float(per_q.mean() / (per_q.std(ddof=1) / np.sqrt(len(per_q))))
    # Regression with quarter fixed effects (demeaned within quarter): fwd NAV change on discount, then adding trailing marks
    D = E.dropna(subset=["nav_chg_trailing"]).copy()
    for c in ["nav_chg_fwd", "pnav", "nav_chg_trailing"]:
        D[c + "_dm"] = D[c] - D.groupby("q")[c].transform("mean")
    def ols(y, X):
        X = np.column_stack(X); b, *_ = np.linalg.lstsq(X, y, rcond=None); res = y - X @ b
        n, k = X.shape; cov = np.linalg.inv(X.T @ X) * (res @ res) / (n - k)
        return b, np.sqrt(np.diag(cov)), 1 - (res @ res) / ((y - y.mean()) @ (y - y.mean()))
    b1, se1, r1 = ols(D.nav_chg_fwd_dm.values, [D.pnav_dm.values])
    b2, se2, r2 = ols(D.nav_chg_fwd_dm.values, [D.nav_chg_trailing_dm.values])
    b3, se3, r3 = ols(D.nav_chg_fwd_dm.values, [D.pnav_dm.values, D.nav_chg_trailing_dm.values])
    out["fe_regression"] = {"n": int(len(D)),
        "pnav_only": {"slope": float(b1[0]), "se": float(se1[0]), "r2": float(r1)},
        "trailing_marks_only": {"slope": float(b2[0]), "se": float(se2[0]), "r2": float(r2)},
        "both": {"slope_pnav": float(b3[0]), "se_pnav": float(se3[0]), "slope_trailing": float(b3[1]), "se_trailing": float(se3[1]), "r2": float(r3)}}
    bk = E.groupby("bucket").agg(n=("pnav", "size"), pnav=("pnav", "median"), nav_chg_fwd=("nav_chg_fwd", "mean"),
                                  nav_chg_fwd_med=("nav_chg_fwd", "median"), share_down5=("nav_chg_fwd", lambda x: float((x < -0.05).mean())),
                                  tr_fwd=("tr_fwd", "mean"), div_yield=("div_yield", "median"))
    out["buckets"] = json.loads(bk.to_json(orient="index"))
    # stability: same fixed-effects slope on the first and second half of the outcome quarters
    qs = sorted(D.q.unique()); mid = qs[len(qs) // 2]
    for nm, part in [("first_half", D[D.q < mid]), ("second_half", D[D.q >= mid])]:
        b, se, r2 = ols(part.nav_chg_fwd_dm.values, [part.pnav_dm.values])
        out["fe_regression"][nm] = {"slope": float(b[0]), "se": float(se[0]), "n": int(len(part)), "from": str(part.q.min().date()), "to": str(part.q.max().date())}
    # Hit-rate of a simple governance flag: discount deeper than 15% -> NAV down more than 5% within a year
    flag = E.pnav < cfg["buckets"][0]; hit = E.nav_chg_fwd < -0.05
    out["flag"] = {"flagged": int(flag.sum()), "precision": float(hit[flag].mean()), "base_rate": float(hit.mean()),
                   "recall": float(flag[hit].mean())}
    # Sector time series (all quarters, including those without a forward outcome yet)
    P = P.assign(cq=P.q.dt.to_period("Q").dt.end_time.dt.normalize())
    S = P.groupby("cq").agg(pnav=("pnav", "median"), nav_chg_fwd=("nav_chg_fwd", "median"), div_yield=("div_yield", "median"), n=("pnav", "size"), avail=("avail", "median"))
    ty = ty.sort_values("d"); S["tbill"] = [ty[ty.d <= a].y3m.iloc[-1] if (ty.d <= a).any() else np.nan for a in S.avail]
    S["spread"] = S.div_yield - S.tbill
    out["sector"] = {k.strftime("%Y-%m-%d"): {c: (None if pd.isna(v) else float(v)) for c, v in r.items() if c != "avail"} for k, r in S.iterrows()}
    return out, E, S


def latest_snapshot(cfg, nav, px, dv, ty):
    """Today's discount: latest public NAV against the latest price."""
    rows = []
    last_px = px.sort_values("d").groupby("symbol").tail(1).set_index("symbol")
    for sym, g in nav.sort_values("q").groupby("symbol"):
        if sym not in last_px.index: continue
        r = g.iloc[-1]; d, p = last_px.loc[sym, "d"], last_px.loc[sym, "close"]
        if any(b > r.q - pd.DateOffset(months=12, days=5) for b in nav_breaks(g, cfg["max_qoq_nav_jump"])): continue
        if (d - r.q).days < cfg["nav_lag_days"] or (d - r.q).days > 200: continue
        dvs = dv[(dv.symbol == sym) & (dv.d > d - pd.Timedelta(days=365)) & (dv.d <= d)]
        rows.append(dict(symbol=sym, nav_date=r.q.strftime("%Y-%m-%d"), price_date=d.strftime("%Y-%m-%d"), pnav=p / r.nav_ps, div_yield=dvs.amount.sum() / p))
    L = pd.DataFrame(rows); lo, hi = cfg["pnav_bounds"]; L = L[(L.pnav > lo) & (L.pnav < hi)]
    t = ty.sort_values("d").y3m.iloc[-1]
    return {"n": int(len(L)), "median_pnav": float(L.pnav.median()), "share_below_085": float((L.pnav < 0.85).mean()),
            "share_below_1": float((L.pnav < 1).mean()), "median_div_yield": float(L.div_yield.median()), "tbill_3m": float(t),
            "price_date": L.price_date.max()}, L


def figures(cfg, E, S, out, L):
    fd = Path(cfg["out_dir"]) / "figures"; fd.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    # 1. sector median P/NAV (top) and the median NAV change that followed (bottom)
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10, 6.4), sharex=True, gridspec_kw={"height_ratios": [1.3, 1]}); x = S.index
    ax.plot(x, S.pnav, color=NAVY, lw=2.2, marker="o"); ax.axhline(1, color=GREY, lw=0.8)
    ax.set_ylabel("Median price / NAV"); ax.set_title("Listed private credit: what the market paid, and what the marks did next", loc="left", fontsize=12)
    ax.annotate(f"{S.pnav.iloc[-1]:.2f}", (x[-1], S.pnav.iloc[-1]), textcoords="offset points", xytext=(6, -4), fontsize=9, color=NAVY)
    v = S.nav_chg_fwd * 100; ax2.bar(x, v.fillna(0), width=50, color=[RED if t < 0 else NAVY for t in v.fillna(0)], alpha=0.8)
    for xi, t in zip(x, v):
        if pd.isna(t): ax2.text(xi, 0.15, "not yet\nknown", ha="center", va="bottom", fontsize=7, color=GREY)
    ax2.axhline(0, color=GREY, lw=0.8); ax2.set_ylabel("Median NAV change,\nnext 4 quarters (%)")
    fig.tight_layout(); fig.savefig(fd / "sector_pnav_vs_nav_change.png", dpi=160); plt.close(fig)
    # 2. scatter, demeaned within quarter
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    xd = E.pnav - E.groupby("q").pnav.transform("mean"); yd = (E.nav_chg_fwd - E.groupby("q").nav_chg_fwd.transform("mean")) * 100
    ax.scatter(xd, yd, s=14, color=NAVY, alpha=0.45)
    b = out["fe_regression"]["pnav_only"]["slope"]; xx = np.linspace(xd.min(), xd.max(), 10); ax.plot(xx, b * xx * 100, color=GOLD, lw=2.2, label=f"Fit: 10 points of P/NAV = {b*10:.1f} points of NAV change")
    ax.axhline(0, color=GREY, lw=0.6); ax.axvline(0, color=GREY, lw=0.6)
    ax.set_xlabel("Price / NAV relative to other BDCs that quarter"); ax.set_ylabel("Next-year NAV change relative to peers (pp)")
    ax.set_title("Cheaper than peers today, marked down more than peers next year", loc="left", fontsize=12); ax.legend(frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(fd / "discount_vs_nav_change.png", dpi=160); plt.close(fig)
    # 3. bucket bars: how often NAV fell more than 5% over the next year
    bk = out["buckets"]; order = ["deep", "modest", "premium"]; lab = ["Below 0.85", "0.85 to 1.00", "1.00 and above"]
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    v = [bk[o]["share_down5"] * 100 for o in order]; bars = ax.bar(lab, v, color=[RED, GOLD, NAVY], width=0.55)
    for bb, val, o in zip(bars, v, order):
        ax.text(bb.get_x() + bb.get_width() / 2, val + 1, f"{val:.0f}%", ha="center", fontsize=12, fontweight="bold")
        ax.text(bb.get_x() + bb.get_width() / 2, val / 2, f"avg NAV change\n{bk[o]['nav_chg_fwd']*100:+.1f}%\nn={bk[o]['n']}", ha="center", va="center", fontsize=9, color="white")
    ax.axhline(out["flag"]["base_rate"] * 100, color=GREY, lw=1, ls="--"); ax.text(2.33, out["flag"]["base_rate"] * 100 + 0.8, "all BDCs", fontsize=8, color=GREY, ha="right")
    ax.set_xlabel("Price / NAV when the NAV became public"); ax.set_ylabel("Share with NAV down >5% next year (%)"); ax.set_ylim(0, max(v) + 8)
    ax.set_title("Discounted BDCs were more likely to be marked down", loc="left", fontsize=12)
    fig.tight_layout(); fig.savefig(fd / "nav_change_by_bucket.png", dpi=160); plt.close(fig)
    # 4. income compensation: dividend yield vs 3m T-bill
    fig, ax = plt.subplots(figsize=(10, 4.6))
    ax.plot(S.index, S.div_yield * 100, color=NAVY, lw=2, marker="o", label="Median trailing dividend yield")
    ax.plot(S.index, S.tbill * 100, color=GREY, lw=2, marker="o", label="3-month Treasury bill")
    ax.fill_between(S.index, S.tbill * 100, S.div_yield * 100, color=GOLD, alpha=0.18, label="Spread")
    ax.set_ylabel("% per year"); ax.set_title("The income cushion over cash", loc="left", fontsize=12); ax.legend(frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(fd / "income_spread.png", dpi=160); plt.close(fig)


def main(cfg_path="configs/default.yaml"):
    cfg = yaml.safe_load(open(cfg_path)); out_dir = Path(cfg["out_dir"]); out_dir.mkdir(exist_ok=True)
    syms, nav, px, dv, sp, ty = load(cfg)
    nav = split_adjust(nav, sp)
    P = build_panel(cfg, nav, px, dv)
    out, E, S = analyse(cfg, P, ty); out["screened_out"] = P.attrs["dropped"]
    snap, L = latest_snapshot(cfg, nav, px, dv, ty); out["latest"] = snap
    # illustrative valuation-governance translation
    slope = out["fe_regression"]["pnav_only"]["slope"]
    out["illustration"] = {"nav_usd": cfg["nav_usd"], "markdown_per_10pt_discount_usd": float(-slope * 0.10 * cfg["nav_usd"])}
    # robustness: a longer publication lag (covers 10-K year-end filings at ~60 days)
    rob = {}
    for lag in cfg.get("robust_lags", []):
        c2 = dict(cfg, nav_lag_days=lag); o2, _, _ = analyse(c2, build_panel(c2, nav, px, dv), ty)
        rob[str(lag)] = {"n": o2["n_obs"], "spearman_by_quarter_mean": o2["spearman_by_quarter_mean"],
                         "positive_quarters": o2["spearman_positive_quarters"], "fe_slope": o2["fe_regression"]["pnav_only"]["slope"],
                         "fe_se": o2["fe_regression"]["pnav_only"]["se"], "deep_nav_chg": o2["buckets"]["deep"]["nav_chg_fwd"],
                         "premium_nav_chg": o2["buckets"]["premium"]["nav_chg_fwd"]}
    out["robustness_lag"] = rob
    P.to_csv(out_dir / "panel.csv", index=False); L.to_csv(out_dir / "latest_snapshot.csv", index=False)
    json.dump(out, open(out_dir / "metrics.json", "w"), indent=1, default=float)
    figures(cfg, E, S, out, L)
    fr = out["fe_regression"]
    print(f"obs {out['n_obs']} BDCs {out['n_bdcs']} quarters {len(out['quarters'])}")
    print("spearman pooled", out["spearman_pooled"], "by-q mean", round(out["spearman_by_quarter_mean"], 3), out["spearman_positive_quarters"], "t", round(out["spearman_t"], 2))
    print("FE pnav only", fr["pnav_only"], "\ntrailing only", fr["trailing_marks_only"], "\nboth", fr["both"])
    for k, v in out["buckets"].items(): print(k, {a: round(b, 4) if isinstance(b, float) else b for a, b in v.items()})
    print("flag", out["flag"]); print("latest", snap); print("illus", out["illustration"]); print("robust", rob)


if __name__ == "__main__":
    main()
