"""Public migration acceptance inputs; no compiler or upgrade implementation."""
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import sdk1
import sdk2
from common import BusinessError, dumps, loads
from samples import EXPECTED, RULE, build_legacy, tables
from warehouse import (Registry, Runner, connect, put_batch, read_publication,
                       storage_version)


def outcome(fn):
    try:
        return ("values", fn())
    except BusinessError as error:
        return ("error", error.code)


def legacy():
    db = connect()
    try:
        build_legacy(db)
        assert storage_version(db) == 1
        assert Runner(db).run("daily", "morning") == EXPECTED
        assert Registry(db).get("price") == RULE
        assert read_publication(db, "daily", "morning") == EXPECTED
        print(dumps({"legacy": EXPECTED, "version": storage_version(db)}))
    finally:
        db.close()


def corpus():
    bad_div = ["div", ["lit", 1], ["lit", 0]]
    lookup = ["lookup", "a", "key"]
    expressions = [RULE["expr"], ["col", "flag"], ["lit", Fraction(2, 7)],
                   lookup, bad_div,
                   ["div", ["lookup", "a", "alternate"], ["lookup", "b", "key"]],
                   ["div", ["lit", None], lookup],
                   ["div", lookup, ["lit", None]],
                   ["div", ["lit", None], ["lit", 0]],
                   ["case", ["eq", lookup, ["lit", 1]], ["lit", 7], ["lit", 8]],
                   ["case", ["col", "flag"], ["lit", None],
                    ["case", ["eq", ["col", "key"], ["lit", 3]],
                     ["lit", 8], ["div", ["lit", 1], ["col", "amount"]]]]]
    for condition in (True, False, None):
        for bad in (bad_div, lookup):
            expressions.extend([["case", ["lit", condition], ["lit", 7], bad],
                                ["case", ["lit", condition], bad, ["lit", 7]]])
    datasets = [tables()]
    selected = tables()
    selected["facts"][0]["flag"] = False
    datasets.append(selected)
    nulls = tables()
    nulls["facts"] = [
        {"key": None, "alternate": 2, "amount": 0, "flag": None},
        {"key": 2, "alternate": 2, "amount": 0, "flag": False},
        {"key": 9, "alternate": None, "amount": None, "flag": True}]
    nulls["a"] = [{"key": None, "value": 9}, {"key": None, "value": 10},
                  {"key": 2, "value": None}, {"key": 2, "value": None}]
    datasets.extend([nulls, {"facts": [], "a": [], "b": []}])
    return [({"sdk": 1, "expr": expr}, data) for expr in expressions for data in datasets]


def canonical(relation):
    assert set(relation.columns) == {"position", "value"}
    rows = sorted(relation.records(), key=lambda row: row["position"])
    assert [row["position"] for row in rows] == list(range(len(rows)))
    return [row["value"] for row in rows]


def forbid_old_reads(action, *args):
    def authorize(operation, table, column, database, trigger):
        import sqlite3
        if operation == sqlite3.SQLITE_READ and table in {"rules_v1", "tasks_v1"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db = args[0]
    db.set_authorizer(authorize)
    try:
        with patch.object(sdk1, "evaluate", side_effect=AssertionError("SDK1 runtime used")):
            return action(*args)
    finally:
        db.set_authorizer(None)


def migrated_work(db, registry, runner, pairs):
    assert registry.get("price")["sdk"] == 2
    assert runner.run("daily", "morning") == EXPECTED
    assert read_publication(db, "daily", "morning") == EXPECTED
    later = tables()
    later["facts"] = [{"key": 2, "alternate": 2, "amount": 4, "flag": False}]
    later["a"] = [{"key": 2, "value": 11}]
    put_batch(db, "later", later)
    assert runner.run("daily", "later") == [Fraction(11, 4)]
    for index, (plan, data, expected) in enumerate(pairs):
        registry.save("probe", plan)
        registry.bind("probe-task", "probe")
        stored = registry.get("probe")
        sdk2.validate(stored)
        put_batch(db, "probe-" + str(index), data)
        actual = outcome(lambda: runner.run("probe-task", "probe-" + str(index)))
        direct = outcome(lambda: canonical(sdk2.run_saved(db, "probe-task", data)))
        assert actual == expected, (index, actual, expected)
        assert direct == expected, (index, direct, expected)
        publication = read_publication(db, "probe-task", "probe-" + str(index))
        assert publication == (expected[1] if expected[0] == "values" else None)
    # Existing success survives a selected error under a replacement rule.
    registry.save("price", {"sdk": 1, "expr": ["lookup", "a", "key"]})
    assert outcome(lambda: runner.run("daily", "morning")) == ("error", "cardinality")
    assert read_publication(db, "daily", "morning") == EXPECTED
    # SDK2-native replacement must be visible through existing warehouse objects.
    native = {"sdk": 2, "nodes": [
        {"id": "f", "op": "scan", "source": "facts",
         "columns": {"amount": "amount"}, "ordinal": "position"},
        {"id": "p", "op": "project", "input": "f", "fields": {
            "position": {"col": "position"}, "value": {"col": "amount"}}}
    ], "output": "p"}
    sdk2.save_plan(db, "price", native)
    assert registry.get("price") == native
    assert runner.run("daily", "morning") == [10, 10, 3, None, 0]
    registry.bind("daily", "half")
    assert runner.run("daily", "morning") == [5, 5, Fraction(3, 2), None, 0]


def verify():
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("original regressions failed")
    from migration import upgrade
    pairs = [(plan, data, outcome(lambda p=plan, d=data: sdk1.evaluate(p, d)))
             for plan, data in corpus()]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "warehouse.sqlite"
        db = connect(path)
        try:
            build_legacy(db)
            assert Runner(db).run("daily", "morning") == EXPECTED
            audit_rules = db.execute("SELECT * FROM rules_v1 ORDER BY rule_id").fetchall()
            audit_tasks = db.execute("SELECT * FROM tasks_v1 ORDER BY task_id").fetchall()
            before = list(db.iterdump())
            def fail():
                assert storage_version(db) == 2
                assert db.execute("SELECT count(*) FROM sdk2_tasks").fetchone()[0] == 2
                raise RuntimeError("migration before commit")
            try:
                upgrade(db, before_commit=fail)
            except RuntimeError as error:
                assert str(error) == "migration before commit"
            else:
                raise AssertionError("before_commit was not invoked")
            assert list(db.iterdump()) == before
            assert Runner(db).run("daily", "morning") == EXPECTED
        finally:
            db.close()
        db = connect(path)
        try:
            registry, runner = Registry(db), Runner(db)
            assert upgrade(db) is None
            assert storage_version(db) == 2
            assert read_publication(db, "daily", "morning") == EXPECTED
            assert db.execute("SELECT * FROM rules_v1 ORDER BY rule_id").fetchall() == audit_rules
            assert db.execute("SELECT * FROM tasks_v1 ORDER BY task_id").fetchall() == audit_tasks
            forbid_old_reads(migrated_work, db, registry, runner, pairs)
            after = list(db.iterdump())
            def no_callback():
                raise AssertionError("idempotent upgrade invoked callback")
            assert upgrade(db, before_commit=no_callback) is None
            assert list(db.iterdump()) == after
            assert db.execute("SELECT * FROM rules_v1 ORDER BY rule_id").fetchall() == audit_rules
            assert db.execute("SELECT * FROM tasks_v1 ORDER BY task_id").fetchall() == audit_tasks
        finally:
            db.close()
        db = connect(path)
        try:
            assert storage_version(db) == 2
            final = forbid_old_reads(
                lambda connection: Runner(connection).run("daily", "morning"), db)
            assert final == [5, 5, Fraction(3, 2), None, 0]
            print(dumps({"migration": "verified", "cases": len(pairs),
                         "reopened_result": final, "storage_version": storage_version(db)}))
        finally:
            db.close()


def demo():
    db = connect()
    try:
        build_legacy(db)
        result_before = Runner(db).run("daily", "morning")
        from migration import upgrade
        upgrade(db)
        result_after = forbid_old_reads(lambda connection: Runner(connection).run("daily", "morning"), db)
        assert result_before == result_after == EXPECTED
        later = tables()
        later["facts"] = [{"key": 2, "alternate": 2, "amount": 4, "flag": False}]
        later["a"] = [{"key": 2, "value": 11}]
        put_batch(db, "later", later)
        changed = forbid_old_reads(lambda connection: Runner(connection).run("daily", "later"), db)
        assert changed == [Fraction(11, 4)]
        selected = tables()
        selected["facts"][0]["flag"] = False
        put_batch(db, "selected", selected)
        error = forbid_old_reads(
            lambda connection: outcome(lambda: Runner(connection).run("daily", "selected")), db)
        assert error == ("error", "cardinality")
        assert read_publication(db, "daily", "selected") is None
        print(dumps({"storage_version": storage_version(db), "before": result_before,
                     "after": result_after, "later": changed, "selected": error,
                     "failed_publication": read_publication(db, "daily", "selected")}))
    finally:
        db.close()


if __name__ == "__main__":
    commands = {"legacy": legacy, "verify": verify, "demo": demo}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit("usage: migration_checks.py legacy|verify|demo")
    commands[sys.argv[1]]()
