"""Recorded successful page extractions; URLs are already normalized."""
from crawler import Crawler

SEEDS = ["s"]
LINKS = {"s": ["a", "b"], "a": ["c"], "b": ["c"], "c": ["d"], "d": ["c"]}


def cases():
    return {
        "shared": {
            "seeds": SEEDS, "links": LINKS,
            "ops": [
                ["claim", "c", "C"], ["claim", "a", "A"],
                ["complete", "A", []], ["claim", "b", "B"], ["complete", "B", []]
            ]},
        "epoch": {
            "seeds": SEEDS,
            "links": {"s": ["a"], "a": ["c"], "c": ["d"], "d": ["c"]},
            "ops": [
                ["claim", "c", "OLD"], ["claim", "s", "CUT"],
                ["complete", "CUT", []], ["claim", "s", "OPEN"],
                ["complete", "OPEN", ["a"]], ["complete", "OLD", ["poison"]],
                ["claim", "c", "NEW"], ["complete", "NEW", []]
            ]},
        "rollback": {
            "seeds": SEEDS, "links": LINKS,
            "ops": [
                ["claim", "a", "A"], ["rollback", "A", ["new"]],
                ["complete", "A", []], ["complete", "A", ["poison"]]
            ]}
    }


def replay(spec):
    crawler = Crawler(spec["seeds"], spec["links"])
    aliases, records = {}, []
    for op in spec["ops"]:
        crawler.store.trace.clear()
        if op[0] == "claim":
            result = crawler.claim(op[1])
            if result is not None:
                aliases[op[2]] = result["token"]
        elif op[0] in ("complete", "rollback"):
            token = aliases.get(op[1], op[1])
            if op[0] == "complete":
                result = crawler.complete(token, op[2])
            else:
                try:
                    with crawler.store.write():
                        accepted = crawler.complete(token, op[2])
                        inside = crawler.inspect()
                        raise RuntimeError("SAMPLE_ROLLBACK")
                except RuntimeError as exc:
                    if str(exc) != "SAMPLE_ROLLBACK":
                        raise
                result = {"attempt": accepted, "inside": inside, "result": "rolled_back"}
        else:
            raise ValueError("BAD_OPERATION")
        records.append({"op": op, "result": result, "state": crawler.inspect(),
                        "reads": list(crawler.store.trace)})
    crawler.store.db.close()
    return records


def growth(size):
    links = {url: list(targets) for url, targets in LINKS.items()}
    if size:
        links["s"].append("u0")
        for i in range(size):
            links["u" + str(i)] = ["u" + str(i + 1)] if i + 1 < size else []
    return {"seeds": SEEDS, "links": links,
            "ops": [["claim", "a", "A"], ["complete", "A", []]]}
