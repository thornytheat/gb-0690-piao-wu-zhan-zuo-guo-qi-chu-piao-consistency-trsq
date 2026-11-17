import sqlite3, json, os
from pathlib import Path
from contextlib import contextmanager
from app.errors import DomainError

# Columns added for batch lineage.  The original batch row is written exactly
# once; supplements are new rows, never in-place updates.
LINEAGE_COLUMNS = {
    "root": "TEXT", "base": "TEXT", "revision": "INTEGER",
    "content": "TEXT", "supplement": "TEXT", "created": "TEXT",
}


def _path():
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path / "runs.sqlite"


def _migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
               "version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    db.execute("CREATE TABLE IF NOT EXISTS lineages (root TEXT PRIMARY KEY, current TEXT NOT NULL)")
    existing = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
    for name, kind in LINEAGE_COLUMNS.items():
        if name in existing:
            continue
        try:
            db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
        except sqlite3.OperationalError as exc:
            # A concurrent connection won the migration race; the column now
            # exists, so continue rather than fail the request.
            if "duplicate column" not in str(exc).lower():
                raise


def _renumber(db):
    rows = db.execute("SELECT id,root,revision FROM runs ORDER BY COALESCE(root,id),"
                      "COALESCE(revision,0),rowid").fetchall()
    seen = {}
    for row in rows:
        root = row["root"] or row["id"]
        seen[root] = seen.get(root, 0) + 1
        db.execute("UPDATE runs SET root=?,revision=? WHERE id=?", (root, seen[root], row["id"]))


def _consistent(db):
    """Cheap read-only check: is any lineage bookkeeping actually broken?"""
    row = db.execute("SELECT 1 FROM runs WHERE root IS NULL OR root='' OR revision IS NULL "
                     "OR revision<1 LIMIT 1").fetchone()
    if row:
        return False
    row = db.execute("SELECT 1 FROM runs r WHERE r.id=r.root AND NOT EXISTS "
                     "(SELECT 1 FROM lineages l WHERE l.root=r.root) LIMIT 1").fetchone()
    if row:
        return False
    row = db.execute("SELECT 1 FROM runs GROUP BY root,revision HAVING COUNT(*)>1 LIMIT 1").fetchone()
    if row:
        return False
    row = db.execute("SELECT 1 FROM lineages l WHERE NOT EXISTS "
                     "(SELECT 1 FROM runs r WHERE r.id=l.current AND r.root=l.root) LIMIT 1").fetchone()
    if row:
        return False
    row = db.execute("SELECT 1 FROM lineages l WHERE l.current IS NOT ("
                     "SELECT r.id FROM runs r WHERE r.root=l.root "
                     "ORDER BY r.revision DESC, r.rowid DESC LIMIT 1) LIMIT 1").fetchone()
    if row:
        return False
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                     "AND name='runs_revision' LIMIT 1").fetchone()
    if not row:
        return False
    row = db.execute("SELECT 1 FROM lineages WHERE root NOT IN (SELECT root FROM runs) LIMIT 1").fetchone()
    return row is None


def _recover(db):
    """Repair lineage bookkeeping after an interrupted or restored write.

    Batch writes are one transaction (new row + pointer), so a process death
    leaves either both or neither.  This pass still protects readers from an
    older database image: every batch gets a root/revision pair, every lineage
    gets a current pointer, and the pointer is reconciled to the highest
    persisted revision of that root.  A failed switch writes no row at all,
    so it is never promoted here.  The common path is a read-only check, so
    ordinary page views never take a write lock.
    """
    if not _consistent(db):
        db.execute("UPDATE runs SET root=id WHERE root IS NULL OR root=''")
        db.execute("UPDATE runs SET revision=1 WHERE revision IS NULL OR revision<1")
        duplicates = db.execute(
            "SELECT 1 FROM runs GROUP BY root,revision HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if duplicates:
            _renumber(db)
        db.execute("INSERT OR IGNORE INTO lineages(root,current) "
                   "SELECT root,id FROM runs r WHERE r.id=r.root AND NOT EXISTS "
                   "(SELECT 1 FROM lineages l WHERE l.root=r.root)")
        db.execute("UPDATE lineages SET current=("
                   "SELECT r.id FROM runs r WHERE r.root=lineages.root "
                   "ORDER BY r.revision DESC, r.rowid DESC LIMIT 1) "
                   "WHERE EXISTS (SELECT 1 FROM runs r WHERE r.root=lineages.root) "
                   "AND current IS NOT (SELECT r.id FROM runs r WHERE r.root=lineages.root "
                   "ORDER BY r.revision DESC, r.rowid DESC LIMIT 1)")
        db.execute("DELETE FROM lineages WHERE root NOT IN (SELECT root FROM runs)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_revision ON runs(root,revision)")


@contextmanager
def connection():
    """Read/write handle with schema and lineage recovery applied."""
    db = sqlite3.connect(_path(), timeout=15)
    db.row_factory = sqlite3.Row
    try:
        _migrate(db)
        _recover(db)
        db.commit()
        yield db
    finally:
        db.close()


@contextmanager
def _transaction():
    db = sqlite3.connect(_path(), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        _migrate(db)
        _recover(db)
        yield db
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.close()


def _payload(row):
    result = json.loads(row["payload"])
    result["version"] = row["version"]
    result["review"] = row["review"]
    revision = row["revision"] or 1
    result["revision"] = revision
    result["root"] = row["root"] or row["id"]
    result["base"] = row["base"]
    manifest = result.setdefault("manifest", {})
    manifest.setdefault("root", result["root"])
    manifest["revision"] = revision
    if row["base"]:
        manifest["base"] = row["base"]
    else:
        manifest.pop("base", None)
    return result


def _row(db, run_id):
    return db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def _current(db, root):
    row = db.execute("SELECT current FROM lineages WHERE root=?", (root,)).fetchone()
    return row["current"] if row else None


def _decorate(db, result):
    root = result["root"]
    run_id = result["manifest"]["run_id"]
    current = _current(db, root)
    result["current"] = current == run_id
    result["current_run"] = current
    result["lineage_size"] = db.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE root=?", (root,)).fetchone()["n"]
    return result


def save(result, content=None):
    """Insert an original batch exactly once (idempotent by content id)."""
    run_id = result["manifest"]["run_id"]
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    created = result["manifest"].get("created", "")
    with _transaction() as db:
        existing = _row(db, run_id)
        if existing is None:
            db.execute("INSERT INTO runs(id,payload,version,review,root,base,revision,content,created) "
                       "VALUES(?,?,1,'待复核',?,NULL,1,?,?)",
                       (run_id, payload, run_id, content, created))
            db.execute("INSERT OR IGNORE INTO lineages(root,current) VALUES(?,?)", (run_id, run_id))
        elif existing["content"] is None and content is not None:
            # A batch stored before raw text was kept: backfill the input only;
            # its payload and evidence stay untouched.
            db.execute("UPDATE runs SET content=? WHERE id=?", (content, run_id))
        return _decorate(db, _payload(_row(db, run_id)))


def fetch(run_id):
    with connection() as db:
        row = _row(db, run_id)
        if row is None:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        return _decorate(db, _payload(row))


def optional(run_id):
    with connection() as db:
        row = _row(db, run_id)
        return None if row is None else _decorate(db, _payload(row))


def fetch_stored(run_id):
    """Persisted result plus the exact replayable input text."""
    with connection() as db:
        row = _row(db, run_id)
        if row is None:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        return _decorate(db, _payload(row)), row["content"]


def history():
    with connection() as db:
        rows = db.execute(
            "SELECT id,version,review,root,base,revision,created FROM runs "
            "ORDER BY rowid DESC LIMIT 100").fetchall()
        current = {r["root"]: r["current"] for r in
                   db.execute("SELECT root,current FROM lineages").fetchall()}
    return [{**dict(row), "current": current.get(row["root"]) == row["id"]} for row in rows]


def lineage(root):
    with connection() as db:
        anchor = _row(db, root)
        if anchor is not None:
            root = anchor["root"] or anchor["id"]
        rows = db.execute(
            "SELECT id,version,review,root,base,revision,created FROM runs "
            "WHERE root=? ORDER BY revision, rowid", (root,)).fetchall()
        if not rows:
            raise DomainError("NOT_FOUND", "批次谱系不存在", 404)
        current = _current(db, root)
    return {"root": root, "current": current,
            "versions": [{**dict(row), "current": row["id"] == current} for row in rows]}


def original_of(root):
    with connection() as db:
        row = db.execute("SELECT * FROM runs WHERE root=? ORDER BY revision, rowid LIMIT 1",
                         (root,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "原批次不存在", 404)
        return _decorate(db, _payload(row))


def save_supplement(result, base_id, content, supplement_text):
    """Create the next revision and switch the lineage in one transaction.

    Either the new row, its revision number and the current pointer all land,
    or nothing changes.  The base row is read, never rewritten.
    """
    run_id = result["manifest"]["run_id"]
    created = result["manifest"].get("created", "")
    with _transaction() as db:
        existing = _row(db, run_id)
        if existing is not None:
            # Replaying the same supplement is a no-op, not a new revision.
            return _decorate(db, _payload(existing))
        base = _row(db, base_id)
        if base is None:
            raise DomainError("NOT_FOUND", "原批次不存在", 404)
        root = base["root"] or base["id"]
        current = _current(db, root)
        if current != base_id:
            raise DomainError("STALE", "原批次已不是当前版本，请基于当前版本补传", 409)
        revision = (base["revision"] or 1) + 1
        result["manifest"]["root"] = root
        result["manifest"]["base"] = base_id
        result["manifest"]["revision"] = revision
        result["revision"] = revision
        result["root"] = root
        result["base"] = base_id
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        try:
            db.execute("INSERT INTO runs(id,payload,version,review,root,base,revision,content,"
                       "supplement,created) VALUES(?,?,1,'待复核',?,?,?,?,?,?)",
                       (run_id, payload, root, base_id, revision, content, supplement_text,
                        created))
            moved = db.execute("UPDATE lineages SET current=? WHERE root=? AND current=?",
                               (run_id, root, base_id))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise DomainError("STALE", "批次版本已被其他补传占用，请刷新后重试", 409)
            raise DomainError("STORE", "批次切换失败，原状态已保留", 503)
        except sqlite3.Error:
            raise DomainError("STORE", "批次切换失败，原状态已保留", 503)
        if moved.rowcount != 1:
            raise DomainError("STALE", "批次当前版本已切换，请刷新后重试", 409)
        return _decorate(db, _payload(_row(db, run_id)))


def review(run_id, version, decision):
    if decision not in ("通过", "退回"):
        raise DomainError("REVIEW", "请选择通过或退回")
    if not isinstance(version, int):
        raise DomainError("STALE", "版本必须为整数")
    with _transaction() as db:
        cur = db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",
                         (decision, run_id, version))
        if cur.rowcount != 1:
            raise DomainError("STALE", "复核版本已更新", 409)
        return _decorate(db, _payload(_row(db, run_id)))
