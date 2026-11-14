import sqlite3
from contextlib import contextmanager


class Store:
    """One in-memory catalog; trace counts logical graph reads, not SQLite pages."""
    def __init__(self, seeds, links):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.trace = []
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE page(url TEXT PRIMARY KEY, eligible INTEGER NOT NULL DEFAULT 0,
                              generation INTEGER NOT NULL DEFAULT 0,
                              pending INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE seed(url TEXT PRIMARY KEY REFERENCES page(url));
            CREATE TABLE edge(src TEXT REFERENCES page(url), dst TEXT REFERENCES page(url),
                              PRIMARY KEY(src,dst));
            CREATE INDEX edge_by_dst ON edge(dst,src);
            CREATE TABLE lease(token TEXT PRIMARY KEY, url TEXT UNIQUE REFERENCES page(url),
                               generation INTEGER NOT NULL);
        """)
        with self.write():
            for url in sorted(set(seeds) | set(links)):
                self.ensure(url)
            for url in sorted(set(seeds)):
                self.db.execute("INSERT INTO seed VALUES(?)", (url,))
            for src, targets in sorted(links.items()):
                self.replace(src, targets)

    @contextmanager
    def write(self):
        outer = not self.db.in_transaction
        if outer:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if outer:
                self.db.execute("COMMIT")
        except BaseException:
            if outer:
                self.db.execute("ROLLBACK")
            raise

    def ensure(self, url):
        self.db.execute("INSERT OR IGNORE INTO page(url) VALUES(?)", (url,))

    def replace(self, src, targets):
        self.ensure(src)
        self.db.execute("DELETE FROM edge WHERE src=?", (src,))
        for dst in sorted(set(targets)):
            self.ensure(dst)
            self.db.execute("INSERT INTO edge VALUES(?,?)", (src, dst))

    def all_graph(self):
        pages = [r[0] for r in self.db.execute("SELECT url FROM page ORDER BY url")]
        edges = [list(r) for r in self.db.execute("SELECT src,dst FROM edge ORDER BY src,dst")]
        self.trace.extend([["all_pages", len(pages)], ["all_edges", len(edges)]])
        return pages, edges

    def outgoing(self, url):
        result = [r[0] for r in self.db.execute(
            "SELECT dst FROM edge WHERE src=? ORDER BY dst", (url,))]
        self.trace.append(["out", url, len(result)])
        return result

    def incoming(self, url):
        result = [r[0] for r in self.db.execute(
            "SELECT src FROM edge WHERE dst=? ORDER BY src", (url,))]
        self.trace.append(["in", url, len(result)])
        return result

    def page(self, url):
        self.trace.append(["page", url])
        row = self.db.execute(
            "SELECT eligible,generation,pending FROM page WHERE url=?", (url,)).fetchone()
        return list(row) if row else None

    def lease(self, token):
        self.trace.append(["lease", token])
        return self.db.execute(
            "SELECT url,generation FROM lease WHERE token=?", (token,)).fetchone()

    def inspect(self):
        # Diagnostic full reads are deliberately outside maintenance trace.
        return {
            "eligible": [r[0] for r in self.db.execute(
                "SELECT url FROM page WHERE eligible=1 ORDER BY url")],
            "pending": [r[0] for r in self.db.execute(
                "SELECT url FROM page WHERE pending=1 ORDER BY url")],
            "active": [dict(zip(("url", "token", "generation"), r)) for r in self.db.execute(
                "SELECT url,token,generation FROM lease ORDER BY url")],
            "links": [list(r) for r in self.db.execute("SELECT src,dst FROM edge ORDER BY src,dst")]
        }
