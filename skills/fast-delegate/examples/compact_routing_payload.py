"""Lossless experimental compaction for repeated structured Jev evidence.

The compact envelope is a transport preview only. Call :func:`expand` before
passing data to any existing parser, evaluator, or routing rule.
"""
from __future__ import annotations

import json
import math

VERSION = 1
MAX_NODES = 100_000
MAX_DEPTH = 256
MAX_BYTES = 64 * 1024 * 1024


def _json(value, depth=0, active=None):
    """Validate JSON-compatible input and reject cyclic/overdeep Python data."""
    if depth > MAX_DEPTH:
        raise ValueError(f"input exceeds maximum depth {MAX_DEPTH}")
    if active is None:
        active = set()
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number is not valid JSON")
        return
    if isinstance(value, (list, dict)):
        ident = id(value)
        if ident in active:
            raise ValueError("cyclic input cannot be encoded as JSON")
        active.add(ident)
        try:
            if isinstance(value, list):
                for item in value:
                    _json(item, depth + 1, active)
            else:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("object keys must be strings")
                    _json(item, depth + 1, active)
        finally:
            active.remove(ident)
        return
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _key(value):
    # Object insertion order is part of this adapter's lossless contract.
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False)


def _count(value, counts):
    if isinstance(value, (dict, list)):
        key = _key(value)
        counts[key] = counts.get(key, 0) + 1
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _count(child, counts)


def compact(value):
    """Encode repeated dict/list subtrees using deterministic local references."""
    _json(value)
    counts = {}
    _count(value, counts)
    repeated = {key for key, count in counts.items() if count > 1}
    ids = {key: f"n{i}" for i, key in enumerate(sorted(repeated))}
    nodes = {}

    def encode(node):
        if isinstance(node, (dict, list)):
            key = _key(node)
            if key in ids:
                node_id = ids[key]
                if node_id not in nodes:
                    nodes[node_id] = encode_inline(node)
                return {"$ref": node_id}
        return encode_inline(node)

    def encode_inline(node):
        if isinstance(node, dict):
            if len(node) == 1 and next(iter(node)) in {"$ref", "$literal"}:
                # Escape every source object that could be mistaken for a tag.
                return {"$literal": [[key, encode(child)] for key, child in node.items()]}
            return {key: encode(child) for key, child in node.items()}
        if isinstance(node, list):
            return [encode(child) for child in node]
        return node

    return {"version": VERSION, "root": encode(value), "nodes": nodes}


def expand(envelope):
    """Expand a compact envelope, rejecting malformed refs and resource abuse."""
    if (not isinstance(envelope, dict) or type(envelope.get("version")) is not int
            or envelope.get("version") != VERSION):
        raise ValueError(f"unsupported compact payload version (expected {VERSION})")
    nodes = envelope.get("nodes")
    if not isinstance(nodes, dict) or len(nodes) > MAX_NODES:
        raise ValueError("invalid or oversized reference table")
    try:
        encoded_size = len(_key(envelope).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid compact payload JSON") from exc
    if encoded_size > MAX_BYTES:
        raise ValueError("compact payload exceeds maximum encoded size")
    active, memo, count = set(), {}, [0]

    def decode(node, depth=0):
        if depth > MAX_DEPTH:
            raise ValueError(f"expanded payload exceeds maximum depth {MAX_DEPTH}")
        count[0] += 1
        if count[0] > MAX_NODES * 20:
            raise ValueError("expanded payload exceeds node limit")
        if isinstance(node, dict):
            if set(node) == {"$ref"}:
                ref = node["$ref"]
                if not isinstance(ref, str) or ref not in nodes:
                    raise ValueError(f"unknown reference: {ref!r}")
                if ref in active:
                    raise ValueError(f"reference cycle at {ref!r}")
                active.add(ref)
                try:
                    return decode(nodes[ref], depth + 1)
                finally:
                    active.remove(ref)
            if set(node) == {"$literal"}:
                pairs = node["$literal"]
                if not isinstance(pairs, list) or not pairs:
                    raise ValueError("invalid literal object escape")
                result, keys = {}, set()
                for pair in pairs:
                    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                        raise ValueError("invalid literal object entry")
                    key = pair[0]
                    if key in keys:
                        raise ValueError(f"duplicate literal object key: {key!r}")
                    keys.add(key)
                    result[key] = decode(pair[1], depth + 1)
                return result
            return {key: decode(child, depth + 1) for key, child in node.items()}
        if isinstance(node, list):
            return [decode(child, depth + 1) for child in node]
        if node is None or isinstance(node, (str, bool, int, float)):
            if isinstance(node, float) and not math.isfinite(node):
                raise ValueError("non-finite number in compact payload")
            return node
        raise ValueError(f"invalid encoded value: {type(node).__name__}")

    if "root" not in envelope:
        raise ValueError("compact payload has no root")
    result = decode(envelope["root"])
    used, pending = set(), list(_referenced(envelope["root"]))
    while pending:
        ref = pending.pop()
        if ref in used:
            continue
        used.add(ref)
        if ref in nodes:
            pending.extend(_referenced(nodes[ref]) - used)
    unused = set(nodes) - used
    if unused:
        raise ValueError(f"unreferenced nodes: {', '.join(sorted(unused)[:5])}")
    _json(result)
    return result


def _referenced(value):
    found = set()
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            found.add(value["$ref"])
        else:
            for child in value.values():
                found.update(_referenced(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_referenced(child))
    return found
