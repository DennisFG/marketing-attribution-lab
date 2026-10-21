"""CSV validation and atomic DuckDB load. Run: python -m src.etl."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
UNATTRIBUTED = "__unattributed__"
SPECS = {
    "touchpoints": ["touchpoint_id", "visitor_id", "touchpoint_at", "channel", "campaign",
                    "utm_source", "utm_medium", "utm_campaign"],
    "conversions": ["conversion_id", "visitor_id", "converted_at", "revenue"],
    "campaign_spend": ["spend_date", "campaign", "channel", "spend"],
}


def normalize(value: str) -> str:
    return re.sub(r"\s+", "_", str(value).strip().lower())


def clean_source(path: Path, source: str):
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(SPECS[source]) - set(raw.columns)
    if missing:
        raise ValueError(f"{path.name}: colunas obrigatórias ausentes: {sorted(missing)}")
    accepted, issues, seen = [], [], set()
    duplicates = rejected = warnings = 0
    for index, record in raw.iterrows():
        row = {key: record[key].strip() for key in SPECS[source]}
        reason, warn = "", []
        try:
            if source == "touchpoints":
                if not row["touchpoint_id"] or not row["visitor_id"]:
                    raise ValueError("identificador ausente")
                stamp = pd.to_datetime(row["touchpoint_at"], utc=True, errors="raise")
                if pd.isna(stamp):
                    raise ValueError("data/hora ausente")
                row["touchpoint_at"] = stamp.tz_convert(None)
                if not row["campaign"]:
                    row["campaign"] = "sem_campanha"
                    warn.append("campanha ausente: sem_campanha")
                if not row["channel"]:
                    row["channel"] = "desconhecido"
                    warn.append("canal ausente: desconhecido")
                key = row["touchpoint_id"]
            elif source == "conversions":
                if not row["conversion_id"] or not row["visitor_id"]:
                    raise ValueError("identificador ausente")
                stamp = pd.to_datetime(row["converted_at"], utc=True, errors="raise")
                if pd.isna(stamp):
                    raise ValueError("data/hora ausente")
                row["converted_at"] = stamp.tz_convert(None)
                key = row["conversion_id"]
            else:
                if not row["campaign"] or not row["channel"]:
                    raise ValueError("campanha/canal ausente no investimento")
                stamp = pd.to_datetime(row["spend_date"], format="%Y-%m-%d", errors="raise")
                if pd.isna(stamp):
                    raise ValueError("data ausente")
                row["spend_date"] = stamp.date()
                key = (row["spend_date"], normalize(row["campaign"]))
            for name in ["campaign", "channel", "utm_source", "utm_medium", "utm_campaign"]:
                if name in row:
                    row[name] = normalize(row[name])
            if row.get("campaign") == UNATTRIBUTED or row.get("channel") == UNATTRIBUTED:
                raise ValueError("identificador reservado para não atribuídos")
            amount = "revenue" if source == "conversions" else "spend" if source == "campaign_spend" else None
            if amount:
                number = float(row[amount])
                if not pd.notna(number) or number in (float("inf"), -float("inf")) or number < 0:
                    raise ValueError("valor monetário inválido/negativo")
                row[amount] = round(number, 2)
        except (ValueError, TypeError, OverflowError) as exc:
            reason = f"registro inválido: {exc}"
        if reason:
            status = "rejected"
            rejected += 1
        elif key in seen:
            status, reason = "duplicate", "chave repetida; mantido o primeiro registro válido"
            duplicates += 1
        else:
            seen.add(key)
            accepted.append(row)
            if not warn:
                continue
            warnings += 1
            status, reason = "warning", "; ".join(warn)
        issues.append({"source": source, "csv_row": index + 2, "status": status,
                       "reason": reason, "raw_record": json.dumps(record.to_dict(), ensure_ascii=False)})
    summary = {"source": source, "filename": path.name, "input_rows": len(raw),
               "accepted_rows": len(accepted), "rejected_rows": rejected,
               "duplicate_rows": duplicates, "warning_rows": warnings}
    return pd.DataFrame(accepted, columns=SPECS[source]), summary, issues


ATTRIBUTION_SQL = """
CREATE TABLE mart_conversion_attribution AS
WITH candidates AS (
    SELECT c.conversion_id, c.visitor_id, c.converted_at, c.revenue,
           t.touchpoint_id, t.touchpoint_at, t.channel, t.campaign,
           ROW_NUMBER() OVER (PARTITION BY c.conversion_id
               ORDER BY t.touchpoint_at ASC, t.touchpoint_id ASC) AS first_rank,
           ROW_NUMBER() OVER (PARTITION BY c.conversion_id
               ORDER BY t.touchpoint_at DESC, t.touchpoint_id ASC) AS last_rank
    FROM stg_conversions c LEFT JOIN stg_touchpoints t
      ON c.visitor_id = t.visitor_id
     AND t.touchpoint_at >= c.converted_at - INTERVAL '7 days'
     AND t.touchpoint_at < c.converted_at
), chosen AS (
    SELECT *, 'first_touch' AS model FROM candidates WHERE first_rank = 1
    UNION ALL
    SELECT *, 'last_touch' AS model FROM candidates WHERE last_rank = 1
)
SELECT conversion_id, visitor_id, converted_at, revenue, model, touchpoint_id,
       touchpoint_at,
       COALESCE(channel, '__unattributed__') AS channel,
       COALESCE(campaign, '__unattributed__') AS campaign
FROM chosen;
CREATE TABLE mart_campaign_attribution AS
SELECT CAST(converted_at AS DATE) AS conversion_date, model, channel, campaign,
       COUNT(*) AS conversions, COUNT(DISTINCT visitor_id) AS converting_visitors,
       SUM(revenue) AS revenue
FROM mart_conversion_attribution GROUP BY ALL;
"""


def run_etl(raw_dir: Path = ROOT / "data/raw", db_path: Path = ROOT / "data/processed/attribution.duckdb"):
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    frames, summaries, all_issues = {}, [], []
    for source in SPECS:
        frame, summary, issues = clean_source(raw_dir / f"{source}.csv", source)
        frames[source] = frame
        summaries.append(summary)
        all_issues.extend(issues)
    mapping = pd.concat([frames["touchpoints"][["campaign", "channel"]],
                         frames["campaign_spend"][["campaign", "channel"]]])
    if not mapping.empty and (mapping.groupby("campaign")["channel"].nunique() > 1).any():
        raise ValueError("Uma campanha não pode ter canais conflitantes entre as fontes.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp = db_path.with_name(f".{db_path.name}.{uuid4().hex}.tmp")
    schemas = {
        "touchpoints": "touchpoint_id VARCHAR, visitor_id VARCHAR, touchpoint_at TIMESTAMP, channel VARCHAR, campaign VARCHAR, utm_source VARCHAR, utm_medium VARCHAR, utm_campaign VARCHAR",
        "conversions": "conversion_id VARCHAR, visitor_id VARCHAR, converted_at TIMESTAMP, revenue DOUBLE",
        "campaign_spend": "spend_date DATE, campaign VARCHAR, channel VARCHAR, spend DOUBLE",
    }
    try:
        with duckdb.connect(str(temp)) as con:
            for source, frame in frames.items():
                con.execute(f"CREATE TABLE stg_{source} ({schemas[source]})")
                if not frame.empty:
                    con.register("incoming", frame)
                    con.execute(f"INSERT INTO stg_{source} SELECT * FROM incoming")
                    con.unregister("incoming")
            con.execute(ATTRIBUTION_SQL)
            summary_frame = pd.DataFrame(summaries)
            con.register("incoming_summary", summary_frame)
            con.execute("CREATE TABLE etl_summary AS SELECT * FROM incoming_summary")
            con.execute("CREATE TABLE etl_issues (source VARCHAR, csv_row INTEGER, status VARCHAR, reason VARCHAR, raw_record VARCHAR)")
            if all_issues:
                issue_frame = pd.DataFrame(all_issues)
                con.register("incoming_issues", issue_frame)
                con.execute("INSERT INTO etl_issues SELECT * FROM incoming_issues")
            con.execute("CREATE TABLE etl_run (loaded_at VARCHAR, window_days INTEGER, synthetic BOOLEAN)")
            con.execute("INSERT INTO etl_run VALUES (?, 7, true)", [datetime.now(timezone.utc).isoformat()])
        os.replace(temp, db_path)
    finally:
        temp.unlink(missing_ok=True)
    return summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw")
    parser.add_argument("--db", type=Path, default=ROOT / "data/processed/attribution.duckdb")
    args = parser.parse_args()
    print(json.dumps(run_etl(args.raw_dir, args.db), indent=2, ensure_ascii=False))
