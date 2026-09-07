# Cycle 0004 — User Prompt Breakdown

Source: [prompts/cycle_0004.json](prompts/cycle_0004.json) · `user` field
Assembled at: 2026-09-03T06:54:23Z · length: 11,209 chars

This documents what the orchestrator packed into the **user message** sent to
`gemini-3.5-flash-lite` for seed cycle 4. The message is built from six labelled
sections plus a closing instruction.

---

## 1. `=== CAMPAIGN STATE ===`

A snapshot of the live AFL++ campaign at trigger time:

- `edges_found: 5858` | `bitmap_cvg: 11.13%` | `corpus_count: 12544` | `saved_crashes: 0` | `saved_hangs: 43`
- No new edge for **0.6 h** across **6 instances** (best_edges=5858, total_crashes=0, total_hangs=338).
- Canned interpretation: coverage growth has stopped; byte-level mutation has
  saturated the current queue, so structurally new documents are needed.
- Stalled mutation operators: `inf` (29×, 0 new-coverage finds).

Purpose: tell the model the campaign is on a plateau and why.

## 2. `=== 3 REAL EXAMPLE SEEDS (shown as format) ===`

Three seeds pulled from the AFL++ queue, "chosen for size/printability and mutual
diversity, not just recency", each with its AFL id line:

| id | op | note |
|---|---|---|
| `id:000478,src:000001,…,op:quick,pos:1,val:+1,+cov` | quick | `<U-3402823669209384…` — long run of the repeated digit string `340282366920938463472597946867209384634725979468672…`, truncated (10,243 chars total) |
| `id:000481,src:000001,…,op:quick,pos:17,val:+1,+cov` | quick | `<a-34028236692093:46347259794686720…` — same digit-blob motif with a `:` inserted, truncated (10,243 chars total) |
| `id:000538,src:000001,…,op:havoc,rep:4,+cov` | havoc | `<a-340282366920938463472597946867209384634725979-><!---9384632…` — digit blob with an unterminated comment, truncated (10,243 chars total) |

Purpose: show the model the *format / shape* of inputs currently in the corpus
(these are degenerate mutation artifacts, not hand-written XML).

## 3. `=== YOUR OWN PRIOR SEEDS THAT DID NOT HELP ===`

Feedback from earlier cycles of this run — seeds the model itself produced that
added **0 new coverage**, with an instruction not to reproduce them or minor
variants. Two representative clusters:

- **AVOID — 4 past seeds (cycles 1,2,3):** default-namespace + prefixed-namespace doc
  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <root xmlns="http://example.com/ns1" xmlns:ns2="http://example.com/ns2">
    <ns2:element ns2:attr="value">Content with namespaces</ns2:element>
  </root>
  ```
- **AVOID — 2 past seeds (cycles 2,3):** `xsi:noNamespaceSchemaLocation` + stylesheet PI
  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="schema.xsd">
    <!-- Processing Instruction with target and data -->
    <?xml-stylesheet type="text/xsl" href="style.xsl"?>
    <data>Valid structure checking schema hints</data>
  </root>
  ```

Purpose: negative examples — steer the model away from paths it already tried.

## 4. `=== A HIGH-COST INPUT (coverage lead) ===`

One queue input that makes the parser burn abnormal CPU (`time:138967994`,
`execs:73421913`, `op:havoc,rep:2`), framed as a hint that nearby structures may
reach unexplored branches — explicitly *not* a bug to reproduce.

Content: a corrupted copy of Sebastian Pipping's **"Parameter Laughs"** file — a
billion-laughs variant using nested parameter entities:

```xml
<?xml version="1.0"?>
<!-- … "Parameter Laughs" … %pe24; takes 3–12 s; %pe40; needs a hard reset … -->
<!DOCTYPE r [
  <!ENTITY % pe_1 "<!---->">
  <!ENTITY % pe_2 "&#37;pe_1;<!---->&#37;pe_1;">
  <!ENTITY % pe_3 "&#37;pe_2;<!---->&#37;pe_2;">
  <!ENTITY % pe_4 "&#37;pe_3;<!-- …very long comment… -->
  …
```

(truncated, 4,992 chars total; comment text is mangled — "CopSebastian", "libexpqt", etc.)

Purpose: point the model at the parameter-entity / DTD-expansion machinery as a
promising area.

## 5. `=== A PREVIOUSLY FLAGGED INPUT (coverage lead) ===`

`(none available this run)` — placeholder section, no data.

## 6. Closing instruction

> Generate 5 new XML document files that are structurally novel compared to the
> examples above — not incremental tweaks of what's already there.
> Before you give the seeds, think step by step about what the current seeds and
> campaign state indicate about the program, the types of inputs in this
> particular file format that are likely to exercise new code paths, and how to
> generate new seeds that are well-formed and valid files that exercise new code
> paths in the target program.
> Produce well-formed or near-well-formed files that reach code the current
> corpus does not. Coverage of untested code is the goal. Output only the fenced
> ``` code blocks — no analysis, no commentary.

---

## What the model returned (for reference)

5 fenced XML blocks: external-DTD entity, parameter-entity → `<!ELEMENT>`,
`encoding="UTF-16"` declaration, nested general entity, and an unparsed
entity + `NOTATION`/`NDATA`. All 5 were later scored `BAD (redundant)` — 0 new
edges. See [cycle_0004.md](cycle_0004.md).
