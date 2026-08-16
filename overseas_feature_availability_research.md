# V10.2 Overseas Feature Availability Research

## HKJC official public overseas pages inspected

| Source | Confirmed pre-race fields | Not confirmed as structured per-runner feature | Engineering decision |
|---|---|---|---|
| HKJC Overseas Race Card, S1 France, 16 Aug 2026 | Race name, class, distance, surface, going, venue, horse number/name, declared weight, jockey, draw, trainer, Career `starts (1st-2nd-3rd)`, Last 5 Run, win rate, Top 3 rate, gear. | RPR, IFHA/World Rating, dated last-run date, per-horse going-by-going records. | Parse and preserve only direct source-labelled fields. Existing career prior remains valid. Add optional rating/date/going-history contract fields but leave unknown null. |
| HKJC overseas jockey/trainer ranking page, 24 Feb 2024 S1 | Country season jockey/trainer starts, wins, places and strike rate, plus page update date. | Group 1-specific trainer starts/wins; an HKJC-verified global trainer Group 1 statistic. Page disclaimer states third-party content is not verified/endorsed by HKJC. | Do not create `trainer_g1_win_rate` from generic seasonal ranking. Allow only explicitly labelled, time-stamped official G1 history to feed the feature; otherwise null/zero signal. |
| HKJC overseas information and fixture pages | Official overseas race-card and fixture navigation. | A complete 2023–2026 structured historical source for rating/going history. | Preserve source URLs and source outcomes; do not impute unavailable cross-jurisdiction history. |

## Strict feature gate

1. `international_rating` may be used only when the official source payload labels a numeric RPR, IFHA, World Rating, or equivalent international rating **and** has a source URL/timestamp. A missing rating contributes neutral relative strength.
2. `days_since_last_run` may be used only when a structured `last_run_date` is declared before model generation; text-only Last 5 Run strings do not establish a date.
3. `going_suitability` may be calculated only from pre-cutoff completed overseas archive rows with identifiable going and official finish results. No archive coverage means neutral (0) log signal.
4. `trainer_g1_win_rate` may be used only from a source explicitly labelled G1/Group 1 with starts, wins and an as-of timestamp. Country seasonal all-race strike-rate is not a substitute.
5. The T-15/T-5 odds-drop signal requires two complete same-race snapshots, horse identity match, valid prices >1 and timestamps. A missing/partial pair produces no adjustment.

## Sources

1. HKJC Overseas Racing Information: https://racing.hkjc.com/en-us/overseas/
2. HKJC overseas jockey/trainer ranking sample: https://racing.hkjc.com/racing/overseas/english/20240224/S1/1/jockey-trainer-ranking.aspx?para=/20240224/S1/1
