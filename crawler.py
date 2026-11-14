from itertools import count
from store import Store


class Crawler:
    """Correct whole-graph reference. No local reachability maintenance."""
    def __init__(self, seeds, links):
        self.store = Store(seeds, links)
        self.serial = count(1)
        with self.store.write():
            self._rebuild()
        self.store.trace.clear()

    def _rebuild(self):
        pages, edges = self.store.all_graph()
        graph = {url: [] for url in pages}
        for src, dst in edges:
            graph[src].append(dst)
        reached = {r[0] for r in self.store.db.execute("SELECT url FROM seed")}
        todo = list(reached)
        while todo:
            for dst in graph[todo.pop()]:
                if dst not in reached:
                    reached.add(dst)
                    todo.append(dst)
        for url in pages:
            old, generation, pending = self.store.page(url)
            new = int(url in reached)
            if old != new:
                self.store.db.execute(
                    "UPDATE page SET eligible=?,generation=?,pending=? WHERE url=?",
                    (new, generation + 1, new, url))
                if not new:
                    self.store.db.execute("DELETE FROM lease WHERE url=?", (url,))

    def claim(self, url=None):
        with self.store.write():
            if self.store.db.execute("SELECT count(*) FROM lease").fetchone()[0] >= 2:
                return None
            if url is None:
                row = self.store.db.execute(
                    "SELECT url,generation FROM page WHERE pending=1 ORDER BY url LIMIT 1").fetchone()
            else:
                row = self.store.db.execute(
                    "SELECT url,generation FROM page WHERE url=? AND pending=1", (url,)).fetchone()
            if row is None:
                return None
            url, generation = row
            token = "t" + str(next(self.serial))
            self.store.db.execute("UPDATE page SET pending=0 WHERE url=?", (url,))
            self.store.db.execute("INSERT INTO lease VALUES(?,?,?)", (token, url, generation))
            return {"url": url, "token": token}

    def complete(self, token, links):
        with self.store.write():
            lease = self.store.lease(token)
            if lease is None:
                return "stale"
            url, generation = lease
            state = self.store.page(url)
            if not state[0] or state[1] != generation:
                return "stale"
            if not isinstance(links, list) or any(
                    not isinstance(dst, str) or not dst for dst in links):
                raise ValueError("BAD_LINKS")
            self.store.db.execute("DELETE FROM lease WHERE token=?", (token,))
            self.store.replace(url, links)
            self._rebuild()
            self.store.db.execute(
                "UPDATE page SET pending=1 WHERE url=? AND eligible=1", (url,))
            return "accepted"

    def inspect(self):
        return self.store.inspect()
