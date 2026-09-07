# Cycle 0004 — `xml-run-20260903-062004`

| Field | Value |
|---|---|
| Cycle ID | 4 |
| Triggered at | 2026-09-03T06:54:21Z |
| Plateau at trigger | 2064 s (~34 min with no new edge) |
| Queue size at trigger | 12,544 |
| LLM model | gemini-3.5-flash-lite |
| LLM finish reason | STOP (1 attempt, not truncated) |
| Seed kind | text / `.xml` |
| Cycle duration | 80.8 s |
| Candidates requested / parsed / materialized / injected | 5 / 5 / 5 / 5 |
| **New coverage** | **0** |
| `afl-addseeds` | invoked, exit 0 |
| Errors | none |

## Target

```
AFLPlus/libxml2-build/xmllint-afl --noout @@
```

libxml2 `xmllint`, coverage-guided AFL++ campaign against `afl-output-libxml2`.

## Campaign state fed into the prompt

- edges_found: 5858 | bitmap_cvg: 11.13% | corpus_count: 12544 | saved_crashes: 0 | saved_hangs: 43
- No new edge for 0.6 h across 6 instances (best_edges=5858, total_crashes=0, total_hangs=338).
- Interpretation supplied: coverage growth has stopped; byte-level mutation has saturated the current queue, structurally new documents are needed.
- Mutation operators no longer paying off: `inf` (29x, 0 new-coverage finds).
- Leads included: one high-CPU "parameter laughs" parameter-entity input; no separately flagged input this run.
- Avoid-list: 4 prior LLM seeds from cycles 1–3 (namespace doc, xsi/schema-hint + PI doc) that re-drove covered paths with 0 new edges.

## Generated candidates

All 5 evaluated as **`BAD (redundant)`** — 0 new edges each.

| # | Focus | total_edges | edge_sig |
|---|---|---|---|
| 0 | External DTD load via `<!ENTITY ext SYSTEM "http://127.0.0.1/nonexistent.dtd">` | 1042 | `57e08c24…` |
| 1 | Internal parameter entity that expands to an `<!ELEMENT>` decl (`%int;`) | 1037 | `4fc4dbf3…` |
| 2 | `encoding="UTF-16"` declaration on an ASCII-bytes document | 728 | `59b25b17…` |
| 3 | Nested general-entity reference (`&ref;` → `&base; Extended`) | 1076 | `7ce862b9…` |
| 4 | Unparsed entity + `<!NOTATION>` with `NDATA`, referenced in content (`&logo;`) | 1096 | `68b6dd0c…` |

### cand_00.xml
```xml
<!DOCTYPE root [
  <!ELEMENT root (item*)>
  <!ELEMENT item (#PCDATA)>
  <!ENTITY ext SYSTEM "http://127.0.0.1/nonexistent.dtd">
]><root><item>External DTD loading</item></root>
```

### cand_01.xml
```xml
<!DOCTYPE test [
  <!ENTITY % int "<!ELEMENT elem (#PCDATA)>">
  %int;
]><test>Parameter entity evaluation</test>
```

### cand_02.xml
```xml
<?xml version="1.0" encoding="UTF-16"?>
<root>
  <data>UTF-16 encoded document</data>
</root>
```

### cand_03.xml
```xml
<!DOCTYPE doc [
  <!ENTITY base "Initial">
  <!ENTITY ref "&base; Extended">
]><doc>&ref;</doc>
```

### cand_04.xml
```xml
<?xml version="1.0"?>
<!DOCTYPE root [
  <!ELEMENT root ANY>
  <!NOTATION png SYSTEM "image/png">
  <!ENTITY logo SYSTEM "logo.png" NDATA png>
]>
<root>&logo;</root>
```

## Outcome

No new coverage. All candidates exercise DTD/entity machinery that the 12.5k-entry
corpus already covers. `xmllint` is run without `--noent` / `--loaddtd` / `--valid`,
so external DTD fetches (cand_00), parameter-entity expansion (cand_01) and unparsed
entities (cand_04) are largely not processed, and the UTF-16 declaration in cand_02
mismatches the actual bytes. Seeds were still injected into the queue for AFL++ to
mutate. Plateau continued into cycle 5.
