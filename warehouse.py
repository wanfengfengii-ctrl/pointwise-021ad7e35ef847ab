"""Version-1 registry, batch intake and atomic result publication."""
import sqlite3
from contextlib import contextmanager

import sdk1
from common import dumps, loads, tables_copy


def connect(path=":memory:"):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def transaction(db, before_commit=None):
    # SAVEPOINT also composes with a caller-owned transaction.
    db.execute("SAVEPOINT warehouse_unit")
    try:
        yield
        if before_commit is not None:
            before_commit()
        db.execute("RELEASE SAVEPOINT warehouse_unit")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT warehouse_unit")
        db.execute("RELEASE SAVEPOINT warehouse_unit")
        raise


def create_legacy(db):
    with transaction(db):
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES('storage_version','1')")
        db.execute("CREATE TABLE rules_v1(rule_id TEXT PRIMARY KEY, ast_json TEXT NOT NULL)")
        db.execute("CREATE TABLE tasks_v1(task_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES rules_v1(rule_id))")
        db.execute("CREATE TABLE batches(batch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
        db.execute("""CREATE TABLE publications(
            task_id TEXT NOT NULL, batch_id TEXT NOT NULL,
            results_json TEXT NOT NULL, PRIMARY KEY(task_id,batch_id))""")


def storage_version(db):
    return int(db.execute("SELECT value FROM meta WHERE key='storage_version'").fetchone()[0])


def _legacy(db):
    if storage_version(db) != 1:
        raise ValueError("this adapter requires storage version 1")


def _name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("nonempty identifier required")


class Registry:
    def __init__(self, db):
        self.db = db

    def save(self, rule_id, plan):
        _name(rule_id)
        sdk1.validate(plan)
        encoded = dumps(plan)
        with transaction(self.db):
            _legacy(self.db)
            self.db.execute("""INSERT INTO rules_v1 VALUES(?,?)
                ON CONFLICT(rule_id) DO UPDATE SET ast_json=excluded.ast_json""",
                            (rule_id, encoded))

    def get(self, rule_id):
        _legacy(self.db)
        row = self.db.execute("SELECT ast_json FROM rules_v1 WHERE rule_id=?", (rule_id,)).fetchone()
        if row is None:
            raise KeyError(rule_id)
        return loads(row[0])

    def bind(self, task_id, rule_id):
        _name(task_id)
        with transaction(self.db):
            self.get(rule_id)
            self.db.execute("""INSERT INTO tasks_v1 VALUES(?,?)
                ON CONFLICT(task_id) DO UPDATE SET rule_id=excluded.rule_id""",
                            (task_id, rule_id))


def put_batch(db, batch_id, tables):
    _name(batch_id)
    payload = dumps(tables_copy(tables))
    with transaction(db):
        old = db.execute("SELECT payload_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if old is not None:
            if old[0] != payload:
                raise ValueError("batch identity conflict")
            return
        db.execute("INSERT INTO batches VALUES(?,?)", (batch_id, payload))


def read_publication(db, task_id, batch_id):
    row = db.execute("SELECT results_json FROM publications WHERE task_id=? AND batch_id=?",
                     (task_id, batch_id)).fetchone()
    return None if row is None else loads(row[0])


class Runner:
    def __init__(self, db):
        self.db = db

    def run(self, task_id, batch_id, *, before_commit=None):
        with transaction(self.db, before_commit):
            _legacy(self.db)
            task = self.db.execute("SELECT rule_id FROM tasks_v1 WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            plan = Registry(self.db).get(task[0])
            batch = self.db.execute("SELECT payload_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            results = sdk1.evaluate(plan, loads(batch[0]))
            self.db.execute("""INSERT INTO publications VALUES(?,?,?)
                ON CONFLICT(task_id,batch_id) DO UPDATE SET results_json=excluded.results_json""",
                            (task_id, batch_id, dumps(results)))
        return results
