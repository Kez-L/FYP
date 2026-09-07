# Cycle 4 → Cycle 5: does the "AVOID" feedback actually work?

Run: `xml-run-20260903-062004` · target `xmllint-afl --noout @@` · model `gemini-3.5-flash-lite`

**Short answer:** the AVOID list stops the model re-emitting the *exact* seed it is
shown, but it does **not** stop the model repeating the same *kind* of seed. Cycle 5
avoided the two literal documents in its AVOID list and still scored **0 new
coverage** — identical outcome to cycles 1–4.

| | C1 | C2 | C3 | C4 | C5 |
|---|---|---|---|---|---|
| new coverage | 0 | 0 | 0 | 0 | 0 |
| candidate statuses | 5× BAD (redundant) | 5× BAD | 5× BAD | 5× BAD | 5× BAD |

---

## What each prompt told the model to avoid

**Cycle 4 AVOID list:**
1. namespace-declaration doc (`xmlns=…` + `xmlns:ns2=…`) — "cycles 1,2,3"
2. `xsi:noNamespaceSchemaLocation` + `<?xml-stylesheet?>` doc — "cycles 2,3"

**Cycle 5 AVOID list:**
1. namespace-declaration doc — "cycles 1,2,3" (unchanged)
2. `<?xml version="1.0" encoding="UTF-16"?>` doc — **"cycles 2,4"**

The cluster #2 slot changed between C4 and C5: the schema/stylesheet doc dropped
out and the UTF-16 doc took its place — and its label says **cycles 2,4**, i.e.
the model had already re-generated that seed in cycle 4, *before* it was ever put
on an AVOID list.

## Did cycle 5 obey its AVOID list? — Yes, literally

- No plain namespace-declaration doc in C5. ✅
- No `encoding="UTF-16"` doc in C5. ✅

## Did cycle 5 avoid cycle 4's actual mistakes? — No

Cycle 4's five seeds were all DTD / entity-machinery documents:

| C4 | structure | result |
|---|---|---|
| cand_00 | external entity `<!ENTITY ext SYSTEM "http://127.0.0.1/…">` | 0 |
| cand_01 | parameter entity `<!ENTITY % int "…"> %int;` | 0 |
| cand_02 | `encoding="UTF-16"` declaration | 0 |
| cand_03 | nested general entity `&ref;` → `&base; Extended` | 0 |
| cand_04 | unparsed entity + `<!NOTATION>` `NDATA` | 0 |

Only cand_02 (UTF-16) reached cycle 5's shown AVOID list. The other four were not
shown, and cycle 5 walked straight back into the same territory:

- **C5 cand_00** = external **parameter** entity (`<!ENTITY % ext-pe SYSTEM "http://…"> %ext-pe;`)
  + internal general entity expansion (`&internal-ent;`). That is C4 cand_00 +
  cand_01 + cand_03 recombined into one file.
- **C5 cand_01** (XInclude + `xi:fallback`) is a near-verbatim repeat of **C1 cand_01** and **C3 cand_02**.
- **C5 cand_02** (XSLT stylesheet) is the same schema/stylesheet territory that was in cluster #2 of the C4 AVOID list.
- **C5 cand_03** (catalog DTD with `<!ATTLIST … ID #REQUIRED>`) ≈ **C2 cand_01/04** and **C3 cand_00**.
- **C5 cand_04** (SVG `linearGradient` + `ellipse`) is a near-verbatim repeat of **C3 cand_04**.

## The recurring rotation

Across all five cycles the model recycles ~8 archetypes:

| archetype | C1 | C2 | C3 | C4 | C5 |
|---|:-:|:-:|:-:|:-:|:-:|
| external entity / DTD (`SYSTEM "http://…"`) | ● | | | ● | ● |
| parameter entity + entity expansion | ● | | | ●● | ● |
| NOTATION + NDATA unparsed entity | ● | ● | ● | ● | |
| XInclude + `xi:fallback` | ● | | ● | | ● |
| `xml-stylesheet` PI / XSLT stylesheet | ● | ● | ● | | ● |
| DOCTYPE catalog/note + ELEMENT + ATTLIST ID | | ●● | ● | | ● |
| SVG linearGradient + ellipse | | | ● | | ● |
| alt encoding (`UTF-16` / `ISO-8859-1`) | | ● | ●● | ● | (avoided) |
| namespace-declaration doc | | ● | | | (avoided) |

The AVOID list removes one row at a time; the model just picks another row from
the same table.

## Why the feedback doesn't help here

1. **Reactive and one cycle behind.** A seed only lands on the list after it has
   scored 0 across ≥2 cycles, and only the top 2 clusters are rendered. Cycle 4
   re-emitted the cycle-2 UTF-16 seed because it wasn't shown in the cycle-4
   prompt yet.
2. **No generalization.** The prompt says "don't produce *this document*," not
   "you keep producing DTD/entity/XInclude/SVG boilerplate and none of it moves
   coverage." The model drops the string and stays in the category. Unflagged
   near-duplicates of C1/C3 seeds sailed through in C5.
3. **The rest of the prompt pulls back toward the mistakes.** The "high-cost
   input" lead is a corrupted billion-laughs / *Parameter Laughs* blob, so the
   prompt keeps pointing at parameter-entity structure — exactly what C5 cand_00
   produces. The "3 real example seeds" are unreadable digit-blob mutation
   artifacts and give no signal about what is actually uncovered.
4. **Target invocation can't reward these seeds anyway.** `xmllint --noout`
   without `--noent --loaddtd --valid --xinclude` does not expand external/param
   entities, does not fetch DTDs, does not process XInclude, does not run XSLT.
   Most archetypes in the rotation are structurally incapable of adding edges, so
   which one gets suppressed is irrelevant.

## Takeaway

The AVOID mechanism works as a literal dedup filter and nothing more. To change
outcomes it would need to (a) feed back the *category* that is failing, not just
the exact bytes, (b) include every prior 0-coverage seed or a structural
signature of them rather than the top-2 clusters, and (c) be paired with example
seeds and leads that actually describe reachable-but-uncovered code. As currently
wired, cycles 1–5 are effectively the same request answered five times, and the
coverage number never moves.
