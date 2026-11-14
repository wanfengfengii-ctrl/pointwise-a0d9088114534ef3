import random
from crawler import Crawler
from samples import cases, replay, growth


def oracle(seeds, edges):
    # Independent repeated edge relaxation, used only for small correctness checks.
    seen = set(seeds)
    while True:
        expanded = seen | {dst for src, dst in edges if src in seen}
        if expanded == seen:
            return sorted(seen)
        seen = expanded


def check():
    shared = replay(cases()["shared"])
    assert shared[2]["state"]["eligible"] == ["a", "b", "c", "d", "s"]
    assert shared[2]["state"]["active"] == [{"url": "c", "token": "t1", "generation": 1}]
    assert shared[-1]["state"]["active"] == []
    assert shared[-1]["state"]["eligible"] == ["a", "b", "s"]
    assert ["c", "d"] in shared[-1]["state"]["links"]
    epoch = replay(cases()["epoch"])
    assert epoch[2]["state"]["eligible"] == ["s"]
    assert epoch[2]["state"]["active"] == []
    assert epoch[4]["state"]["eligible"] == ["a", "c", "d", "s"]
    assert epoch[5]["result"] == "stale"
    assert epoch[5]["state"] == epoch[4]["state"]
    assert epoch[-1]["state"]["eligible"] == ["a", "c", "s"]
    rollback = replay(cases()["rollback"])
    assert rollback[1]["result"]["attempt"] == "accepted"
    assert "new" in rollback[1]["result"]["inside"]["eligible"]
    assert rollback[1]["state"] == rollback[0]["state"]
    assert rollback[-1]["result"] == "stale"

    crawler = Crawler(["s"], {"s": ["a"], "a": []})
    token = crawler.claim("s")["token"]
    before = crawler.inspect()
    try:
        crawler.complete(token, [None])
    except ValueError as exc:
        assert str(exc) == "BAD_LINKS"
    else:
        raise AssertionError("bad links accepted")
    assert crawler.inspect() == before
    assert crawler.complete("unknown", [None]) == "stale"
    assert crawler.complete(token, ["a", "a"]) == "accepted"
    assert crawler.inspect()["links"] == [["s", "a"]]
    assert crawler.claim("absent") is None
    assert crawler.claim("a") is not None
    assert crawler.claim("s") is not None
    assert crawler.claim() is None
    crawler.store.db.close()

    crawler = Crawler(["s"], {"s": []})
    try:
        with crawler.store.write():
            cancelled = crawler.claim()["token"]
            raise RuntimeError("ROLLBACK_CLAIM")
    except RuntimeError:
        pass
    fresh = crawler.claim()["token"]
    assert fresh != cancelled
    assert crawler.complete(cancelled, []) == "stale"
    assert crawler.complete(fresh, []) == "accepted"
    crawler.store.db.close()

    rng = random.Random(27)
    for _ in range(12):
        urls = ["s", "a", "b", "c", "d", "e"]
        links = {url: [v for v in urls if rng.randrange(4) == 0] for url in urls}
        crawler = Crawler(["s"], links)
        for _ in range(25):
            before = crawler.inspect()
            url = rng.choice(before["eligible"])
            token = crawler.claim(url)["token"]
            targets = [v for v in urls if rng.randrange(4) == 0]
            assert crawler.complete(token, targets) == "accepted"
            state = crawler.inspect()
            assert state["eligible"] == oracle(["s"], state["links"])
            assert state["pending"] == state["eligible"]
            assert state["active"] == []
        crawler.store.db.close()
    small = replay(growth(0))[-1]["reads"]
    large = replay(growth(1000))[-1]["reads"]
    assert ["all_pages", 5] in small
    assert ["all_pages", 1005] in large
    return "BASELINE_OK: shared entry, isolated cycle, stale epoch, rollback, random oracle, full-scan growth"


if __name__ == "__main__":
    print(check())
