"""Recompute scalar catalog proxies from a sanitized paired-results JSON."""
import json
import math
import sys
from pathlib import Path

def replay(path):
    data = json.loads(Path(path).read_text())
    total = 0.0
    for phase in data["phases"]:
        summed = 0.0
        for request in phase["requests"]:
            i, k, o = (request[x] for x in
                       ("input_tokens", "cached_input_tokens", "output_tokens"))
            rate = request["rates_per_token"]
            cost = (i-k)*rate["input"] + k*rate["cache_read"] + o*rate["output"]
            assert math.isclose(cost, request["cache_aware_proxy"], abs_tol=1e-12)
            assert request["tier"] == ("above_272k" if i > 272000 else "base")
            summed += cost
        assert math.isclose(summed, phase["cache_aware_proxy"], abs_tol=1e-12)
        for key, value in phase["totals"].items():
            if value is not None:
                assert sum(r[key] for r in phase["requests"]) == value
        total += summed
    total += sum(r["input_only_proxy"] for r in data["routing"])
    print(f"Known cache-aware subtotal (unmeasured costs excluded): ${total:.8f}")

if __name__ == "__main__":
    replay(sys.argv[1])
