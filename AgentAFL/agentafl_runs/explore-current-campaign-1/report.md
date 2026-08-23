# AgentAFL Run Report — explore-current-campaign-20260806-042625

Campaign root: `/home/user/Documents/afl-output-libxml2` (instance `main`)  
Model: `gemini-3.5-flash-lite`  |  Started: 2026-08-06T04:26:25.197892+00:00

## Summary

- LLM calls: **5**
- Candidates generated: **25**
- Well-formed rate: **60.0%** (15/25)
- Usefulness rate (GOOD / total): **12.0%**
- Seeds injected: **3** (2 confirmed synced into main's queue, 1 not yet observed synced)
- Total new edges attributable to injected seeds at evaluation time: **6**
- Status breakdown: GOOD (novel coverage)=3, BAD (redundant)=22

## Coverage over time

<svg xmlns="http://www.w3.org/2000/svg" width="960" height="320" font-family="sans-serif">
<rect x="0" y="0" width="960" height="320" fill="#fcfcfb"/>
<line x1="64" y1="280.0" x2="930" y2="280.0" stroke="#e5e4df" stroke-width="1"/>
<text x="56" y="284.0" text-anchor="end" font-size="11" fill="#52514e">5,844</text>
<line x1="64" y1="218.5" x2="930" y2="218.5" stroke="#e5e4df" stroke-width="1"/>
<text x="56" y="222.5" text-anchor="end" font-size="11" fill="#52514e">5,845</text>
<line x1="64" y1="157.0" x2="930" y2="157.0" stroke="#e5e4df" stroke-width="1"/>
<text x="56" y="161.0" text-anchor="end" font-size="11" fill="#52514e">5,847</text>
<line x1="64" y1="95.5" x2="930" y2="95.5" stroke="#e5e4df" stroke-width="1"/>
<text x="56" y="99.5" text-anchor="end" font-size="11" fill="#52514e">5,848</text>
<line x1="64" y1="34.0" x2="930" y2="34.0" stroke="#e5e4df" stroke-width="1"/>
<text x="56" y="38.0" text-anchor="end" font-size="11" fill="#52514e">5,850</text>
<text x="64.0" y="296" text-anchor="middle" font-size="11" fill="#52514e">0h</text>
<text x="497.0" y="296" text-anchor="middle" font-size="11" fill="#52514e">1h</text>
<text x="930.0" y="296" text-anchor="middle" font-size="11" fill="#52514e">2h</text>
<line x1="97.3" y1="34" x2="97.3" y2="280" stroke="#0ca30c" stroke-width="1" stroke-dasharray="2,2"/>
<circle cx="97.3" cy="34" r="4" fill="#0ca30c"/>
<text x="97.3" y="28" text-anchor="middle" font-size="10" fill="#0ca30c">c1</text>
<line x1="303.5" y1="34" x2="303.5" y2="280" stroke="#0ca30c" stroke-width="1" stroke-dasharray="2,2"/>
<circle cx="303.5" cy="34" r="4" fill="#0ca30c"/>
<text x="303.5" y="28" text-anchor="middle" font-size="10" fill="#0ca30c">c2</text>
<line x1="509.2" y1="34" x2="509.2" y2="280" stroke="#fab219" stroke-width="1" stroke-dasharray="2,2"/>
<circle cx="509.2" cy="34" r="4" fill="#fab219"/>
<text x="509.2" y="28" text-anchor="middle" font-size="10" fill="#fab219">c3</text>
<line x1="714.7" y1="34" x2="714.7" y2="280" stroke="#fab219" stroke-width="1" stroke-dasharray="2,2"/>
<circle cx="714.7" cy="34" r="4" fill="#fab219"/>
<text x="714.7" y="28" text-anchor="middle" font-size="10" fill="#fab219">c4</text>
<line x1="920.2" y1="34" x2="920.2" y2="280" stroke="#fab219" stroke-width="1" stroke-dasharray="2,2"/>
<circle cx="920.2" cy="34" r="4" fill="#fab219"/>
<text x="920.2" y="28" text-anchor="middle" font-size="10" fill="#fab219">c5</text>
<polyline points="64.3,280.0 97.6,280.0 130.9,280.0 164.2,280.0 197.5,280.0 230.8,280.0 264.1,280.0 297.4,280.0 330.7,280.0 364.0,116.0 397.3,75.0 430.6,75.0 463.8,75.0 497.1,75.0 530.5,75.0 563.7,75.0 597.0,75.0 630.4,75.0 663.6,75.0 696.9,75.0 730.2,34.0 763.5,34.0 796.9,34.0 830.1,34.0 863.4,34.0 896.7,34.0 930.0,34.0" fill="none" stroke="#2a78d6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
<circle cx="64.3" cy="280.0" r="4" fill="#2a78d6" stroke="#fcfcfb" stroke-width="2"/>
<text x="64.3" y="270.0" text-anchor="start" font-size="11" fill="#0b0b0b">5,844</text>
<circle cx="930.0" cy="34.0" r="4" fill="#2a78d6" stroke="#fcfcfb" stroke-width="2"/>
<text x="930.0" y="24.0" text-anchor="end" font-size="11" fill="#0b0b0b">5,850</text>
<text x="480.0" y="314" text-anchor="middle" font-size="11" fill="#52514e">Hours elapsed since run start</text>
</svg>

*Green marker = cycle injected ≥1 candidate. Amber = cycle fired but injected 0. X-axis is hours elapsed since run start, not wall-clock — directly comparable to a differently-scheduled baseline run's own `plateau_log.csv` at the same snapshot cadence.*

## Per-seed detail

| Cycle | Timestamp | # | Filename | Status | Well-formed | New edges | Injected | Main queue id | Resolution | Downstream (cov descendants) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2026-08-06T04:31:25.301637+00:00 | 0 | cand_00.xml | GOOD (novel coverage) | True | 1 | ✓ | 012425 | orchestrator | 0/2 |
| 1 | 2026-08-06T04:31:25.301637+00:00 | 1 | cand_01.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 1 | 2026-08-06T04:31:25.301637+00:00 | 2 | cand_02.xml | GOOD (novel coverage) | True | 1 | ✓ | - | unresolved | n/a |
| 1 | 2026-08-06T04:31:25.301637+00:00 | 3 | cand_03.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 1 | 2026-08-06T04:31:25.301637+00:00 | 4 | cand_04.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 2 | 2026-08-06T05:02:25.464570+00:00 | 0 | cand_00.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 2 | 2026-08-06T05:02:25.464570+00:00 | 1 | cand_01.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 2 | 2026-08-06T05:02:25.464570+00:00 | 2 | cand_02.xml | GOOD (novel coverage) | True | 4 | ✓ | 012428 | orchestrator | 1/1 |
| 2 | 2026-08-06T05:02:25.464570+00:00 | 3 | cand_03.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 2 | 2026-08-06T05:02:25.464570+00:00 | 4 | cand_04.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 3 | 2026-08-06T05:33:21.195726+00:00 | 0 | cand_00.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 3 | 2026-08-06T05:33:21.195726+00:00 | 1 | cand_01.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 3 | 2026-08-06T05:33:21.195726+00:00 | 2 | cand_02.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 3 | 2026-08-06T05:33:21.195726+00:00 | 3 | cand_03.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 3 | 2026-08-06T05:33:21.195726+00:00 | 4 | cand_04.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 4 | 2026-08-06T06:04:14.860665+00:00 | 0 | cand_00.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 4 | 2026-08-06T06:04:14.860665+00:00 | 1 | cand_01.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 4 | 2026-08-06T06:04:14.860665+00:00 | 2 | cand_02.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 4 | 2026-08-06T06:04:14.860665+00:00 | 3 | cand_03.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 4 | 2026-08-06T06:04:14.860665+00:00 | 4 | cand_04.xml | BAD (redundant) | True | 0 |  | - | n/a | n/a |
| 5 | 2026-08-06T06:35:08.455694+00:00 | 0 | cand_00.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 5 | 2026-08-06T06:35:08.455694+00:00 | 1 | cand_01.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 5 | 2026-08-06T06:35:08.455694+00:00 | 2 | cand_02.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 5 | 2026-08-06T06:35:08.455694+00:00 | 3 | cand_03.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
| 5 | 2026-08-06T06:35:08.455694+00:00 | 4 | cand_04.xml | BAD (redundant) | False | 0 |  | - | n/a | n/a |
