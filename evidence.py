import json
import re
import sys
from pathlib import Path
from checks import check
from samples import cases, replay, growth


def observe():
    counts = []
    for size in (0, 1000):
        reads = replay(growth(size))[-1]["reads"]
        counts.append({
            "size": size,
            "reads": {
                "all_pages": sum(r[1] for r in reads if r[0] == "all_pages"),
                "all_edges": sum(r[1] for r in reads if r[0] == "all_edges"),
                "page_queries": sum(r[0] == "page" for r in reads),
                "lease_queries": sum(r[0] == "lease" for r in reads)
            }
        })
    return {"cases": {name: replay(spec) for name, spec in cases().items()},
            "growth": counts}


def verify_document(text):
    marker = "<!-- crawl-evidence -->"
    if text.count(marker) != 1:
        raise ValueError("EVIDENCE_MARKER")
    block = re.search(r"~~~json\s*\n(.*?)\n~~~", text.split(marker, 1)[1], re.S)
    if block is None:
        raise ValueError("EVIDENCE_BLOCK")
    data = json.loads(block.group(1))
    actual = observe()
    if data["actual"] != actual:
        raise ValueError("ACTUAL_MISMATCH")
    if set(data["predicted"]) != set(actual["cases"]):
        raise ValueError("CASE_COVERAGE")
    for name, records in actual["cases"].items():
        expected_steps = [i for i, row in enumerate(records, 1)
                          if row["op"][0] != "claim"]
        predictions = data["predicted"][name]
        if [row["step"] for row in predictions] != expected_steps:
            raise ValueError("STEP_COVERAGE")
        for prediction in predictions:
            state = records[prediction["step"] - 1]["state"]
            for key in ("eligible", "pending", "active"):
                if prediction[key] != state[key]:
                    raise ValueError("PREDICTED_STATE_MISMATCH")
            if not all(isinstance(prediction[key], list) for key in ("region", "boundary")):
                raise ValueError("REGION_FORMAT")
            if not isinstance(prediction["why"], str) or not prediction["why"].strip():
                raise ValueError("MISSING_REASON")
    if [row["size"] for row in data["locality"]] != [0, 1000]:
        raise ValueError("LOCALITY_COVERAGE")
    for row in data["locality"]:
        for key in ("region", "boundary", "page_reads", "out_reads", "in_reads"):
            if not isinstance(row[key], list):
                raise ValueError("LOCALITY_FORMAT")
        if not isinstance(row["why"], str) or not row["why"].strip():
            raise ValueError("MISSING_COST_REASON")
    refs = re.findall(r"\b(store|crawler|samples|checks|evidence)\.py:([1-9][0-9]*)", text)
    if not {"store", "crawler"} <= {name for name, _ in refs}:
        raise ValueError("SOURCE_REFS")
    for name, number in refs:
        if int(number) > len(Path(name + ".py").read_text(encoding="utf-8").splitlines()):
            raise ValueError("SOURCE_LINE_RANGE")
    return "DOCUMENT_EVIDENCE_OK: observations and declared predictions replayed; design needs semantic review"


def main(argv):
    if argv == ["baseline"]:
        print(check())
    elif argv == ["observe"]:
        print(json.dumps(observe(), ensure_ascii=False, indent=2))
    elif argv == ["verify"]:
        print(check())
        print(verify_document(Path("DESIGN.md").read_text(encoding="utf-8")))
    else:
        raise SystemExit("usage: evidence.py baseline|observe|verify")


if __name__ == "__main__":
    main(sys.argv[1:])
