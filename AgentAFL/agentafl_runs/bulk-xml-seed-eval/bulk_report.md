# Bulk XML seed-generation report — bulk-xml-seed-eval-20260820-071051

- Campaign: `/home/user/Documents/afl-output-libxml2` (instance `main`)
- Model: `gemini-3.5-flash-lite`, temperature 0.9
- 50 LLM calls × up to 10 seeds requested each
- Injection: OFF (evaluation-only)

## Overall

- Requested **500** candidates, model's fenced blocks parsed to **500** (100.0%)
- **283/500** well-formed XML (56.6%)
- **0/500** GOOD (novel coverage) -- usefulness rate **0.0%**
- Response truncated (MAX_TOKENS) in 0/50 cycles (0.0%)
- Parsed-block count didn't match the request in 0/50 cycles (0.0%)

### Status breakdown

| Status | Count | % of total |
|---|---|---|
| BAD (redundant) | 500 | 100.0% |

## Per-construct breakdown

Constructs the prompt explicitly asks the model to prioritize (`build_context.py`'s `CONSTRUCTS`), plus candidates hitting none of them.

| Construct | # candidates | Well-formed rate | Usefulness rate |
|---|---|---|---|
| DOCTYPE | 500 | 56.6% | 0.0% |
| ENTITY | 499 | 56.5% | 0.0% |
| CDATA | 500 | 56.6% | 0.0% |
| xmlns | 500 | 56.6% | 0.0% |
| PI | 500 | 56.6% | 0.0% |
| comment | 500 | 56.6% | 0.0% |
| (none) | 0 | 0.0% | 0.0% |

## Qualitative samples

### GOOD (novel coverage) (0 shown)

_none_

### BAD (redundant) (5 shown)

**cycle 1 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent-param "<!ENTITY ent-val 'Parameter Expanded'>">
  %ent-param;
  <!ENTITY ent-general "General &ent-val;">
  <!NOTATION png SYSTEM "image/png">
  <!ENTITY ext-ent SYSTEM "external.png" NDATA png>
]>
<?xml-stylesheet type="text/xsl" href="transform.xsl"?>
<root xmlns="http://example.com/ns/root" xmlns:sub="http://example.com/ns/sub">
  <!-- Top-level comment testing parser handling before CDATA and entities -->
  <sub:child sub:attr="&ent-general;">
    <![CDATA[
      CDATA section containing unescaped markup <child> &ent-general; </child>
    ]]>
  </sub:child>
</root>
```

**cycle 11 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent "<!ENTITY internal 'Expanded Entity Value'>">
  %ent;
  <!ENTITY ext SYSTEM "nonexistent.ent">
  <!NOTATION png SYSTEM "image/png">
  <?xml-stylesheet type="text/xsl" href="transform.xsl"?>
]>
<?target-pi data="processing instruction value with <tags>"?>
<root xmlns="http://example.org/ns/base" xmlns:sub="http://example.org/ns/sub">
  <!-- Main configuration comment block containing entities: &internal; -->
  <sub:child id="1">
    <![CDATA[
      CDATA section with embedded XML tags: <child>test</child> &internal;
    ]]>
  </sub:child>
  <child>&internal;</child>
</root>
```

**cycle 21 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent "<!ENTITY internal 'Expanded Value'>">
  %ent;
  <!ENTITY external SYSTEM "file:///etc/passwd">
  <!NOTATION png SYSTEM "image/png">
  <?xml-stylesheet type="text/xsl" href="transform.xsl"?>
]>
<root xmlns="http://example.com/ns" xmlns:sub="http://example.com/sub">
  <!-- Top level comment before processing instruction and CDATA -->
  <?target data="value"?>
  <sub:child sub:id="1">
    <![CDATA[
      CDATA section containing <tags>, &internal;, and entities.
    ]]>
  </sub:child>
  <child>&internal;</child>
</root>
```

**cycle 31 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent-param "<!ENTITY ent-val 'Parameter Expanded'>">
  %ent-param;
  <!ENTITY ent-general "General &ent-val;">
  <!NOTATION png SYSTEM "image/png">
  <!ENTITY logo SYSTEM "logo.png" NDATA png>
]>
<?xml-stylesheet type="text/xsl" href="transform.xsl"?>
<root xmlns="http://example.com/ns" xmlns:sub="http://example.com/sub" xmlns:xlink="http://www.w3.org/1999/xlink">
  <!-- Complex root comment with CDATA inside comment: <!-- test --> -->
  <sub:child xlink:type="simple" xlink:href="http://example.com">
    <?sub:pi target="data" processing="true"?>
    <![CDATA[
      CDATA section containing markup <child>&ent-general;</child>
    ]]>
  </sub:child>
  <child>&ent-gener
...[truncated]...
```

**cycle 41 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent "<!ENTITY internal 'Expanded value'>">
  %ent;
  <!ENTITY ext SYSTEM "nonexistent.ent">
  <!NOTATION png SYSTEM "image/png">
  <!ENTITY binary SYSTEM "logo.png" NDATA png>
]>
<?xml-stylesheet type="text/xsl" href="transform.xsl"?>
<root xmlns="http://example.org/ns1" xmlns:sub="http://example.org/ns2" xmlns:xlink="http://www.w3.org/1999/xlink">
  <!-- Comment containing a PI and CDATA-like text: <?pi?> <tag> -->
  <sub:child xlink:type="simple">
    <![CDATA[
      CDATA block with entity reference &internal; and XML markup <child>test</child>
    ]]>
  </sub:child>
  <child>&internal;</child>
</root>
```

### CRASH (0 shown)

_none_

### TIMEOUT (0 shown)

_none_

### NO_COVERAGE (0 shown)

_none_

### malformed (any status) (5 shown)

**cycle 1 / cand_07.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE suite [
  <!ELEMENT suite (test)*>
  <!ELEMENT test (#PCDATA)>
  <!ENTITY test-ent "TestEntity">
  <!NOTATION txt SYSTEM "text/plain">
  <!ENTITY external-txt "notes.txt" NDATA txt>
]>
<?xml-test-runner engine="v1" mode="strict"?>
<suite xmlns="http://suite.example.org" xmlns:h="http://suite.example.org/h">
  <!-- Comment inside suite element -->
  <h:test h:name="suite-test">
    <![CDATA[
      <h:test>CDATA block with &test-ent;</h:test>
    ]]>
  </h:test>
</suite>
```

**cycle 2 / cand_00.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root [
  <!ELEMENT root (child)*>
  <!ELEMENT child (#PCDATA)>
  <!ENTITY % ent "<!ENTITY internal 'parsed data'>">
  %ent;
  <!NOTATION png SYSTEM "image/png">
  <!ENTITY ext SYSTEM "logo.png" NDATA png>
]>
<?xml-stylesheet type="text/xsl" href="transform.xsl"?>
<root xmlns="http://example.org/ns/base" xmlns:sub="http://example.org/ns/sub">
  <!-- Main entry point comment covering structure -->
  <sub:child id="1">
    <![CDATA[
      Some raw CDATA text with <tags> & entities like &internal;
    ]]>
  </sub:child>
  <child>&internal;</child>
</root>
```

**cycle 2 / cand_01.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE catalog [
  <!ELEMENT catalog (item+)>
  <!ELEMENT item (#PCDATA)>
  <!ENTITY base-ent "Standard Value">
  <!ENTITY complex-ent "&base-ent; and More">
  <!ENTITY % param-ent "<!ENTITY nested-ent 'Nested Value'>">
  %param-ent;
]>
<?xml-processing-instruction target="validator" status="active"?>
<catalog xmlns="http://schema.example.com/catalog" xmlns:x="http://schema.example.com/catalog/x">
  <!-- Comment explaining item structure -->
  <x:item code="A1">
    <![CDATA[
      <x:item>CDATA section containing <markup> and &complex-ent;</x:item>
    ]]>
  </x:item>
  <item>&nested-ent;</item>
</catalog>
```

**cycle 2 / cand_03.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE archive [
  <!ELEMENT archive (record)*>
  <!ELEMENT record (#PCDATA)>
  <!ENTITY note "Archival record">
  <!ENTITY % dtd-macro "<!ENTITY macro-ent 'Macro Text'>">
  %dtd-macro;
  <!NOTATION pdf SYSTEM "application/pdf">
  <!ENTITY doc-file "file.pdf" NDATA pdf>
]>
<?xml-stylesheet type="text/css" href="style.css"?>
<archive xmlns="http://archive.org/ns" xmlns:arc="http://archive.org/ns/arc">
  <!-- Archive description comment -->
  <arc:record id="rec-01">
    <?arc-action type="index"?>
    <![CDATA[
      Archive CDATA payload containing <xml-fragment> &macro-ent;</xml-fragment>
    ]]>
  </arc:record>
  <record>&macro-ent;</record>
</archive>
```

**cycle 2 / cand_04.xml** (new_edges=0, constructs=['CDATA', 'DOCTYPE', 'ENTITY', 'PI', 'comment', 'xmlns'])
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE container [
  <!ELEMENT container (data)*>
  <!ELEMENT data (#PCDATA)>
  <!ENTITY alpha "Alpha">
  <!ENTITY beta "&alpha; Beta">
  <!ENTITY % ext-macro "<!ENTITY delta 'Delta Entity'>">
  %ext-macro;
]>
<?xml-stylesheet href="layout.xsl" type="text/xsl"?>
<container xmlns="http://container.org/ns" xmlns:v1="http://container.org/ns/v1">
  <!-- Container level comment -->
  <v1:data id="d1">
    <?v1-instruction process="true"?>
    <![CDATA[
      Unparsed text node: &beta; and &delta; with characters < > &
    ]]>
  </v1:data>
  <data>&delta;</data>
</container>
```

## Cycle-level errors

_none_
