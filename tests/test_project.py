from datetime import date
import hashlib
from pathlib import Path

import pandas as pd
import pytest

from src.analytics import read_data, report, safe_ratio
from src.etl import SPECS, UNATTRIBUTED, run_etl
from src.sample_data import generate


def touch(id="t1", visitor="v1", at="2026-10-10T10:00:00Z", campaign="a", channel="email"):
    return dict(touchpoint_id=id, visitor_id=visitor, touchpoint_at=at, campaign=campaign,
                channel=channel, utm_source=channel, utm_medium="demo", utm_campaign=campaign)


def conversion(id="c1", visitor="v1", at="2026-10-12T10:00:00Z", revenue=200):
    return dict(conversion_id=id, visitor_id=visitor, converted_at=at, revenue=revenue)


def cost(campaign="a", channel="email", value=20, day="2026-10-12"):
    return dict(spend_date=day, campaign=campaign, channel=channel, spend=value)


def build(tmp_path, touches=None, conversions=None, costs=None):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    rows = [touches if touches is not None else [touch()],
            conversions if conversions is not None else [conversion()],
            costs if costs is not None else [cost()]]
    for name, records in zip(SPECS, rows):
        pd.DataFrame(records, columns=SPECS[name]).to_csv(raw / f"{name}.csv", index=False)
    db = tmp_path / "demo.duckdb"
    run_etl(raw, db)
    return read_data(db), db


def view(data, **filters):
    return report(data, date(2026, 10, 12), date(2026, 10, 12), **filters)


def test_first_and_last(tmp_path):
    data, _ = build(tmp_path, [touch(), touch("t2", at="2026-10-11T10:00:00Z", campaign="b")],
                    costs=[cost(), cost("b")])
    rows = data["mart_conversion_attribution"].set_index("model")
    assert rows.loc["first_touch", "campaign"] == "a"
    assert rows.loc["last_touch", "campaign"] == "b"
    assert len(rows) == 2


@pytest.mark.parametrize("timestamp,eligible", [
    ("2026-10-05T10:00:00Z", True),
    ("2026-10-05T09:59:59Z", False),
    ("2026-10-12T10:00:00Z", False),
    ("2026-10-12T10:00:01Z", False),
])
def test_window(tmp_path, timestamp, eligible):
    data, _ = build(tmp_path, [touch(at=timestamp)])
    rows = data["mart_conversion_attribution"]
    assert rows.touchpoint_id.notna().all() == eligible
    if not eligible:
        assert set(rows.campaign) == {UNATTRIBUTED}


def test_ties_are_deterministic(tmp_path):
    data, _ = build(tmp_path, [touch("z"), touch("a", campaign="b")])
    assert set(data["mart_conversion_attribution"].touchpoint_id) == {"a"}


def test_unattributed_and_missing_visitor(tmp_path):
    data, _ = build(tmp_path, [], [conversion()])
    rows = data["mart_conversion_attribution"]
    assert rows.touchpoint_id.isna().all()
    assert len(rows) == 2
    assert view(data)["metrics"].unattributed.tolist() == [1, 1]


def test_quality_counts_and_dedup(tmp_path):
    data, _ = build(tmp_path, [touch(), touch(), touch("bad", at="not-a-date")],
                    [conversion(), conversion()], [cost(), cost()])
    summary = data["etl_summary"].set_index("source")
    assert summary.loc["touchpoints", "accepted_rows"] == 1
    assert summary.loc["touchpoints", "rejected_rows"] == 1
    assert summary.loc["touchpoints", "duplicate_rows"] == 1
    assert ((summary.accepted_rows + summary.rejected_rows + summary.duplicate_rows) == summary.input_rows).all()
    assert len(data["mart_conversion_attribution"]) == 2
    assert view(data)["metrics"].investment.tolist() == [20, 20]


def test_filters_do_not_recompute_paths_and_costs_stay_fixed(tmp_path):
    data, _ = build(tmp_path, [touch(), touch("t2", at="2026-10-11T10:00:00Z", campaign="b")],
                    costs=[cost(), cost("b", value=30)])
    assert view(data)["metrics"].investment.tolist() == [50, 50]
    subset = view(data, campaigns=["a"])["metrics"]
    assert subset.conversions.tolist() == [1, 0]
    assert subset.investment.tolist() == [20, 20]
    assert subset.leads.tolist() == [1, 1]


def test_missing_and_partial_cost(tmp_path):
    data, _ = build(tmp_path, costs=[])
    row = view(data)["metrics"].iloc[0]
    assert pd.isna(row.investment) and pd.isna(row.cac) and pd.isna(row.roas)
    data, _ = build(tmp_path, [touch(), touch("t2", visitor="v2", campaign="b")], costs=[cost()])
    row = view(data)["metrics"].iloc[0]
    assert pd.isna(row.investment) and row.known_investment == 20
    assert row.missing_costs == "b"


def test_explicit_zero_cost_and_no_conversions(tmp_path):
    data, _ = build(tmp_path, costs=[cost(value=0)])
    row = view(data)["metrics"].iloc[0]
    assert row.cac == 0 and pd.isna(row.roas)
    data, _ = build(tmp_path, conversions=[])
    row = view(data)["metrics"].iloc[0]
    assert pd.isna(row.cac) and row.roas == 0
    assert safe_ratio(1, 0) is None
    assert safe_ratio(None, 2) is None


def test_empty_inputs(tmp_path):
    data, _ = build(tmp_path, [], [], [])
    assert data["mart_conversion_attribution"].empty
    assert view(data)["metrics"].leads.tolist() == [0, 0]


def test_leads_are_unique_and_mart_reconciles(tmp_path):
    data, _ = build(tmp_path, [touch(), touch("t2", campaign="b")],
                    [conversion(), conversion("c2", at="2026-10-12T11:00:00Z")],
                    [cost(), cost("b")])
    rows = view(data)["metrics"]
    assert rows.leads.tolist() == [1, 1]
    assert rows.conversions.tolist() == [2, 2]
    assert rows.converted_leads.tolist() == [1, 1]
    assert rows.conversion_rate.tolist() == [2, 2]
    mart = data["mart_campaign_attribution"]
    assert mart.groupby("model").conversions.sum().tolist() == [2, 2]
    assert mart.groupby("model").revenue.sum().tolist() == [400, 400]


def test_bad_schema_preserves_previous_database(tmp_path):
    data, db = build(tmp_path)
    digest = hashlib.sha256(db.read_bytes()).digest()
    (tmp_path / "raw/touchpoints.csv").write_text("wrong\nx\n")
    with pytest.raises(ValueError, match="colunas obrigatórias"):
        run_etl(tmp_path / "raw", db)
    assert hashlib.sha256(db.read_bytes()).digest() == digest


def test_conflicting_channel_preserves_previous_database(tmp_path):
    _, db = build(tmp_path)
    digest = hashlib.sha256(db.read_bytes()).digest()
    pd.DataFrame([cost(channel="organic")]).to_csv(tmp_path / "raw/campaign_spend.csv", index=False)
    with pytest.raises(ValueError, match="canais conflitantes"):
        run_etl(tmp_path / "raw", db)
    assert hashlib.sha256(db.read_bytes()).digest() == digest


def test_normalization_and_missing_campaign(tmp_path):
    data, _ = build(tmp_path, [touch(campaign="", channel="")], costs=[])
    row = data["stg_touchpoints"].iloc[0]
    assert row.campaign == "sem_campanha" and row.channel == "desconhecido"
    assert data["etl_summary"].warning_rows.sum() == 1
    data, _ = build(tmp_path, [touch(campaign=" Campaign A ", channel=" Paid Search ")],
                    costs=[cost("campaign_a", "paid_search")])
    assert data["stg_touchpoints"].iloc[0].campaign == "campaign_a"


def test_sample_is_reproducible(tmp_path):
    generate(tmp_path / "one")
    generate(tmp_path / "two")
    for name in SPECS:
        assert (tmp_path / f"one/{name}.csv").read_bytes() == (tmp_path / f"two/{name}.csv").read_bytes()


def test_streamlit_smoke(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    _, db = build(tmp_path)
    monkeypatch.setenv("ATTRIBUTION_DB", str(db))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert len(app.metric) == 8
    app.sidebar.selectbox[0].set_value("last_touch").run()
    assert not app.exception
    app.sidebar.multiselect[1].set_value(["a"]).run()
    assert not app.exception


def test_filtered_totals_reconcile_and_use_conversion_date(tmp_path):
    data, _ = build(tmp_path,
        [touch(), touch("t2", at="2026-10-11T10:00:00Z", campaign="b", channel="organic")],
        [conversion(), conversion("c2", at="2026-10-13T12:00:00Z", revenue=500)],
        [cost(), cost("b", "organic", 30)])
    result = view(data, channels=["email"])
    for row in result["metrics"].itertuples():
        selected = result["campaigns"].loc[result["campaigns"].model == row.model]
        assert selected.conversions.sum() == row.conversions
        assert selected.revenue.sum() == row.revenue
    # c2 is outside the requested conversion date, but early contacts remain eligible.
    assert set(result["detail"].conversion_id) == {"c1"}
    assert result["metrics"].leads.tolist() == [1, 1]


def test_invalid_amounts_and_blank_ids(tmp_path):
    data, _ = build(tmp_path, [touch(), touch("t2", visitor="")],
                    [conversion(), conversion("bad", revenue=-1), conversion("nan", revenue="NaN")],
                    [cost(), cost("b", value=-2)])
    summary = data["etl_summary"].set_index("source")
    assert summary.loc["touchpoints", "rejected_rows"] == 1
    assert summary.loc["conversions", "rejected_rows"] == 2
    assert summary.loc["campaign_spend", "rejected_rows"] == 1


def test_ui_reports_incomplete_cost_and_no_filter_results(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    _, db = build(tmp_path, [touch()], costs=[])
    monkeypatch.setenv("ATTRIBUTION_DB", str(db))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert app.metric[4].value == "N/A"
    assert app.metric[5].value == "N/A"


def test_empty_ui_has_no_exception(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    _, db = build(tmp_path, [], [], [])
    monkeypatch.setenv("ATTRIBUTION_DB", str(db))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert "não possui conversões" in app.info[0].value


def test_end_to_end_synthetic_acceptance(tmp_path):
    generate(tmp_path / "raw")
    db = tmp_path / "demo.duckdb"
    run_etl(tmp_path / "raw", db)
    data = read_data(db)
    result = report(data, date(2026, 10, 4), date(2026, 10, 31))
    metrics = result["metrics"]
    assert metrics.leads.tolist() == [120, 120]
    assert metrics.conversions.tolist() == [80, 80]
    assert metrics.unattributed.tolist() == [2, 2]
    assert metrics.investment.tolist() == [8432, 8432]
    assert metrics.revenue.tolist() == pytest.approx([25792, 25792])
    assert len(data["mart_conversion_attribution"]) == 164
