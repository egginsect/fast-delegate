# Experimental compact routing payload

`compact_routing_payload.py` is a lossless data adapter for previewing a compact
representation of repeated structured evidence in a Jev request. It does not
invoke Jev, dispatch workers, or change routing policy.

```python
from compact_routing_payload import compact, expand

wire_preview = compact(full_request)
request_for_existing_code = expand(wire_preview)
# Pass request_for_existing_code to the existing parser/evaluator path.
```

The versioned envelope stores a root plus a deterministic table of repeated
dict/list subtrees. Repeated structures become local `$ref` objects. Source
objects that could look like internal `$ref` or `$literal` tags are escaped as
ordered key/value pairs, so arbitrary unknown fields cannot collide with the
encoding. Null, false, zero, empty values, Unicode, unknown fields, list order,
each object's insertion order, and per-model overrides remain intact. Objects
with different key orders are not merged as duplicates. `expand` validates the
version, reference table, references, cycles, JSON values, and resource bounds,
then reconstructs a fresh JSON value. Call expansion before any existing
parser, evaluator, or routing rules. Reference IDs are assigned deterministically
from ordered subtree encodings.

The offline tests run all six committed full-inventory requests through the
adapter, compare full JSON values, replay saved answers through the existing
evaluator, and report UTF-8 serialized bytes before and after, including the
reference envelope overhead. These byte counts measure serialized JSON size;
they do not establish API token or cost savings. Successful round-trip proves
only that this adapter can reconstruct the original request. Sending the
compact envelope itself to a live model could change its interpretation and
Jev judgment, so this example makes no claim of equal live judgments.

The format is experimental, JSON-only, and bounded to finite JSON numbers,
string object keys, depth 256, a 64 MiB envelope, and a bounded expansion node
count. It is not a production wire protocol or routing/API change.
