#!/usr/bin/env python3
"""
xml_utils.py — Shared primitives used by build_context.py, evaluate_seeds.py, and
agentafl_orchestrator.py. Factored out so is_well_formed_xml doesn't drift out of
sync between the two places it used to be duplicated (build_context.py took bytes
and used ET.fromstring; evaluate_seeds.py took a Path and used ET.parse — same
underlying expat parser, different call signature).
"""

import xml.etree.ElementTree as ET
from pathlib import Path


def is_well_formed_xml(data) -> bool:
    """Canonical well-formedness check. Accepts bytes, str, or a Path/PathLike:

      - bytes/str: parsed directly via ET.fromstring.
      - Path (or anything with __fspath__, or a plain path string that isn't
        itself well-formed XML — see note below): read from disk first, then
        parsed the same way.

    To keep this a strict drop-in for both prior call sites without a type
    branch that could misfire (a Path is also a valid str-like), callers pass
    a Path object for the file case and bytes/str for the in-memory case —
    exactly what both existing call sites already do.
    """
    if isinstance(data, Path):
        try:
            data = data.read_bytes()
        except OSError:
            return False
    try:
        ET.fromstring(data)
        return True
    except ET.ParseError:
        return False
    except Exception:
        return False


def format_afl_id(n: int) -> str:
    """AFL's zero-padded 6-digit id format, e.g. 123 -> '000123'."""
    return f"{n:06d}"


def parse_queue_filename(name: str) -> dict:
    """
    Parse an AFL++ queue/addseeds filename into its comma-separated fields.
    Handles both shapes seen in real campaign data:

      id:000000,time:0,execs:0,orig:754947.xml
      id:000499,src:000001,time:31400,execs:52369,op:quick,pos:6046,val:+1
      id:012227,sync:addseeds,src:000002,+cov

    Critically distinguishes 'sync:' from 'src:' — build_context.py's FNAME_RE
    does not capture 'sync' at all, and conflating them is wrong: in
    'id:012227,sync:addseeds,src:000002,+cov', src:000002 is an id *inside
    addseeds's own queue*, not a main-queue id. That distinction is exactly
    what the orchestrator's two-stage queue-id resolution depends on.

    Returns:
      {
        "id": str | None,          # this entry's own id, e.g. "012227"
        "src": list[str],          # parent id(s); supports comma/plus multi-parent
                                    # syntax (e.g. "src:000001+000002"), though no
                                    # multi-parent entries have been observed in
                                    # real campaign data so far
        "op": str | None,
        "sync": str | None,        # source instance name if this is a synced entry
        "orig": str | None,        # original filename if this is a seed/addseeds entry
        "has_cov": bool,
        "raw": str,                # the original filename, unchanged
      }
    """
    fields = {
        "id": None, "src": [], "op": None, "sync": None,
        "orig": None, "has_cov": "+cov" in name, "raw": name,
    }
    for part in name.split(","):
        part = part.strip()
        if part == "+cov":
            continue
        if ":" not in part:
            continue
        key, _, val = part.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "id":
            fields["id"] = val
        elif key == "src":
            # supports multi-parent 'src:000001+000002' or 'src:000001,000002'
            # (the latter is ambiguous with the outer comma-split, but AFL++
            # itself only ever emits '+'-joined multi-parent src fields in
            # practice — comma-splitting here just means each such fragment
            # that happens to parse as a bare digit run also gets treated as
            # a parent, which is the conservative/inclusive behavior we want).
            fields["src"].extend(p for p in val.split("+") if p)
        elif key == "op":
            fields["op"] = val
        elif key == "sync":
            fields["sync"] = val
        elif key == "orig":
            fields["orig"] = val
    return fields


def find_synced_entries_bulk(main_queue_dir: Path, sync_source: str,
                              local_ids: set) -> dict:
    """
    One directory listing + parse pass over main_queue_dir, returning
    {local_id: parsed_fields} for every id in local_ids that has already been
    synced in from sync_source (matched via 'sync:<sync_source>,src:<local_id>').

    Deliberately a bulk/batch primitive rather than a per-id lookup: both the
    orchestrator's per-poll resolution check and generate_report.py's
    end-of-run fallback scan have a *list* of pending ids to check against one
    shared directory listing, and main's queue routinely has 12k+ entries —
    paying that O(queue size) cost once per scan (not once per pending id)
    is the difference between a cheap poll and a very slow one.
    """
    if not local_ids:
        return {}
    found = {}
    try:
        entries = list(main_queue_dir.iterdir())
    except OSError:
        return {}
    for f in entries:
        if not f.is_file():
            continue
        fields = parse_queue_filename(f.name)
        if fields["sync"] != sync_source:
            continue
        for src_id in fields["src"]:
            if src_id in local_ids and src_id not in found:
                found[src_id] = fields
    return found
