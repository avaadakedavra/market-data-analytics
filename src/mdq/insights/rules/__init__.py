"""The shipped pattern rules — one module each, discovered by `pkgutil`.

| module | pattern | on the real sample |
|---|---|---|
| `dormancy_staleness` | flat bars are sessions that were not trading | **yes** — 14,152 bars |
| `carried_forward_settlement` | settlement outside a carried-forward range | **yes** — 39 of 43 |
| `feed_incident_cluster` | one date breaks a whole family at once | **yes** — 6 incidents |
| `gap_time_of_day` | intraday holes cluster in one hour of the day | no — they are spread |
| `gap_session_edge` | intraday holes cluster on one weekday | no — they are spread |
| `duplicate_shape` | duplicates are re-deliveries, or contradictions | no — sample has 0 |
| `holiday_calendar` | exchange-wide absences are holidays, not holes | **yes** — 388 sessions |
| `zero_volume_range` | untraded bars that still printed a range | no — all 400 dormant |
| `schema_concentration` | bad input piles up in one column or one file | no — sample has 0 |
| `settlement_reconciliation` | the daily close is a settlement, so skip it | not until WP8 |

The "fires on the real sample?" column is measured, not aspirational, and each module
repeats the honest version in its own docstring with the numbers behind it. Six of the
ten rules do not fire on this vendor's sample, for three different and equally legitimate
reasons: the defect is genuinely absent (duplicates, bad input), the defect is present but
fully explained by the activity model (zero volume with a range — all 400 in dormant
sessions), or the pattern is real but the clustering is not there to find (the intraday
holes are spread right across the overnight session, exactly as thin liquidity looks).
Inventing a way to make any of them appear would make the tool lie about the data.
"""
