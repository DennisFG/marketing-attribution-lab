"""Generate deterministic, synthetic CSVs; never calls an external service."""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.etl import ROOT, SPECS


def generate(output: Path):
    rng = random.Random(42)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    catalog = [("busca_concursos", "paid_search"), ("social_lancamento", "paid_social"),
               ("crm_revisao", "email"), ("conteudo_organico", "organic")]
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    touches, conversions, spend = [], [], []
    for n in range(1, 121):
        visitor = f"v{n:03}"
        first = base + timedelta(days=rng.randrange(0, 18), hours=rng.randrange(8, 20))
        for step in range(rng.randint(1, 3)):
            campaign, channel = catalog[(n + step) % len(catalog)]
            touches.append({"touchpoint_id": f"t{n:03}_{step}", "visitor_id": visitor,
                            "touchpoint_at": (first + timedelta(days=step)).isoformat(),
                            "channel": channel, "campaign": campaign,
                            "utm_source": channel, "utm_medium": "demo",
                            "utm_campaign": campaign})
        if n % 3 != 0:
            conversions.append({"conversion_id": f"c{n:03}", "visitor_id": visitor,
                                "converted_at": (first + timedelta(days=3, hours=1)).isoformat(),
                                "revenue": rng.choice([199.90, 299.90, 499.90])})
    conversions.extend([
        {"conversion_id": "c_sem_contato", "visitor_id": "sem_contato", "converted_at": "2026-10-15T12:00:00Z", "revenue": 250},
        {"conversion_id": "c_fora_janela", "visitor_id": "v001", "converted_at": "2026-10-31T12:00:00Z", "revenue": 300},
    ])
    for day in range(31):
        for campaign, channel in catalog:
            spend.append({"spend_date": (base + timedelta(days=day)).date().isoformat(),
                          "campaign": campaign, "channel": channel,
                          "spend": 0 if channel == "organic" else rng.randrange(50, 151)})
    # Intentional defects demonstrate rejection/deduplication without changing accepted data.
    touches.append(touches[0].copy())
    touches.append({**touches[1], "touchpoint_id": "t_invalid", "touchpoint_at": "invalid-date"})
    conversions.append(conversions[0].copy())
    spend.append({**spend[0], "spend_date": "2026-11-01", "spend": -20})
    for name, rows in [("touchpoints", touches), ("conversions", conversions), ("campaign_spend", spend)]:
        pd.DataFrame(rows, columns=SPECS[name]).to_csv(output / f"{name}.csv", index=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/raw")
    generate(parser.parse_args().output)
