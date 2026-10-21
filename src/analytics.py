"""Shared dashboard calculations. No totals are built by summing distinct counts."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

from src.etl import UNATTRIBUTED

MODELS = ("first_touch", "last_touch")
LABELS = {"first_touch": "Primeiro contato", "last_touch": "Último contato"}


def read_data(db: Path):
    tables = ["stg_touchpoints", "stg_conversions", "stg_campaign_spend",
              "mart_conversion_attribution", "mart_campaign_attribution",
              "etl_summary", "etl_issues", "etl_run"]
    with duckdb.connect(str(db), read_only=True) as con:
        return {name: con.execute(f"SELECT * FROM {name}").df() for name in tables}


def safe_ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def group_filter(frame, channels, campaigns):
    mask = pd.Series(True, index=frame.index)
    if channels:
        mask &= frame["channel"].isin(channels)
    if campaigns:
        mask &= frame["campaign"].isin(campaigns)
    return frame.loc[mask].copy()


def report(data, start, end, channels=None, campaigns=None):
    """Date cohort = conversion dates; lead pool includes its 7-day lookback.

    Attribution is computed BEFORE date/channel/campaign filters. Costs are
    selected by spend date, never joined once per conversion or touchpoint.
    """
    start = pd.Timestamp(start).normalize()
    stop = pd.Timestamp(end).normalize() + timedelta(days=1)
    if stop <= start:
        raise ValueError("O fim do período deve ser igual ou posterior ao início.")
    touches = data["stg_touchpoints"]
    lead_pool = touches.loc[(touches.touchpoint_at >= start - timedelta(days=7)) &
                            (touches.touchpoint_at < stop)]
    lead_pool = group_filter(lead_pool, channels, campaigns)
    spend = data["stg_campaign_spend"].copy()
    spend_dates = pd.to_datetime(spend.spend_date)
    spend = group_filter(spend.loc[(spend_dates >= start) & (spend_dates < stop)], channels, campaigns)
    detail = data["mart_conversion_attribution"]
    cohort = detail.loc[(detail.converted_at >= start) & (detail.converted_at < stop)]
    detail = group_filter(cohort, channels, campaigns)
    # Costs and leads use the same universe in BOTH attribution models.
    pairs = pd.concat([lead_pool[["campaign", "channel"]], spend[["campaign", "channel"]],
                       detail[["campaign", "channel"]]]).drop_duplicates()
    rows, metrics = [], []
    for model in MODELS:
        selected = detail.loc[detail.model == model]
        for campaign, channel in pairs.itertuples(index=False, name=None):
            events = selected.loc[selected.campaign == campaign]
            leads = lead_pool.loc[lead_pool.campaign == campaign, "visitor_id"].nunique()
            costs = spend.loc[spend.campaign == campaign]
            known_cost = not costs.empty
            cost = float(costs.spend.sum()) if known_cost else None
            assigned = campaign != UNATTRIBUTED
            count = len(events) if assigned else 0
            revenue = float(events.revenue.sum()) if assigned else 0.0
            rows.append({"model": model, "campaign": campaign, "channel": channel,
                         "leads": leads if assigned else None, "conversions": count,
                         "unattributed": len(events) if not assigned else 0,
                         "revenue": revenue, "investment": cost,
                         "cost_available": known_cost,
                         "conversion_rate": safe_ratio(count, leads) if assigned else None,
                         "cac": safe_ratio(cost, count), "roas": safe_ratio(revenue, cost)})
        assigned_events = selected.loc[selected.touchpoint_id.notna()]
        required_campaigns = set(lead_pool.campaign) | set(assigned_events.campaign)
        missing_costs = sorted(required_campaigns - set(spend.campaign))
        known_sum = float(spend.spend.sum()) if not spend.empty else None
        total_cost = known_sum if not missing_costs else None
        leads = int(lead_pool.visitor_id.nunique())
        count = len(assigned_events)
        revenue = float(assigned_events.revenue.sum())
        metrics.append({"model": model, "leads": leads, "conversions": count,
                        "converted_leads": int(assigned_events.visitor_id.nunique()),
                        "unattributed": int(selected.touchpoint_id.isna().sum()),
                        "revenue": revenue, "investment": total_cost,
                        "known_investment": known_sum, "missing_costs": ", ".join(missing_costs),
                        "conversion_rate": safe_ratio(count, leads),
                        "cac": safe_ratio(total_cost, count), "roas": safe_ratio(revenue, total_cost)})
    return {"metrics": pd.DataFrame(metrics), "campaigns": pd.DataFrame(rows),
            "detail": detail, "leads": lead_pool, "spend": spend}
