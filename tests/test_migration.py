"""Additional migration-layer assertions; the original suites are unchanged."""
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import sdk1
import sdk2
from common import BusinessError
from compiler import compile_plan
from migration import upgrade
from samples import EXPECTED, RULE, build_legacy, tables
from warehouse import (Registry, Runner, connect, put_batch, read_publication,
                       storage_version)


def rule(expr):
    return {"sdk": 1, "expr": expr}


def compiled(rule_obj):
    plan = compile_plan(rule_obj)
    sdk2.validate(plan)
    return plan


class CompilerSemanticsTests(unittest.TestCase):
    def _check(self, expr, data):
        plan = rule(expr)
        legacy = sdk1.evaluate(plan, data)
        relation = sdk2.execute(compiled(plan), data)
        self.assertEqual(relation.columns, ("position", "value"))
        rows = sorted(relation.records(), key=lambda item: item["position"])
        self.assertEqual([item["position"] for item in rows], list(range(len(data["facts"]))))
        self.assertEqual([item["value"] for item in rows], legacy)

    def test_duplicate_facts_keep_identity_and_order(self):
        data = tables()
        self._check(RULE["expr"], data)
        plan = compiled(RULE)
        relation = sdk2.execute(plan, data)
        positions = [row["position"] for row in relation.records()]
        self.assertEqual(sorted(positions), list(range(5)))

    def test_lookup_null_duplicates_are_cardinality(self):
        data = {"facts": [{"key": 2, "alternate": 2, "amount": 1, "flag": True}],
                "a": [{"key": 2, "value": None}, {"key": 2, "value": None}], "b": []}
        plan = compiled(rule(["lookup", "a", "key"]))
        with self.assertRaises(BusinessError) as caught:
            sdk2.execute(plan, data)
        self.assertEqual(caught.exception.code, "cardinality")

    def test_null_key_never_matches_even_null_lookup_keys(self):
        data = {"facts": [{"key": None, "alternate": None, "amount": None, "flag": None}],
                "a": [{"key": None, "value": 9}, {"key": None, "value": 10}], "b": []}
        self._check(["lookup", "a", "key"], data)

    def test_lazy_case_unselected_bad_branch_is_silent(self):
        bad = ["div", ["lit", 1], ["lit", 0]]
        data = {"facts": [{"key": 1, "alternate": 1, "amount": 1, "flag": True}],
                "a": [{"key": 1, "value": 2}, {"key": 1, "value": 3}], "b": []}
        self._check(["case", ["lit", True], ["lit", 7], bad], data)
        with self.assertRaises(BusinessError):
            sdk2.execute(compiled(rule(["case", ["lit", True], bad, ["lit", 7]])), data)

    def test_null_operand_does_not_mask_evaluated_lookup_error(self):
        data = {"facts": [{"key": 1, "alternate": 1, "amount": 1, "flag": True}],
                "a": [{"key": 1, "value": None}, {"key": 1, "value": None}], "b": []}
        for expr in (["div", ["lit", None], ["lookup", "a", "key"]],
                     ["div", ["lookup", "a", "key"], ["lit", None]]):
            with self.subTest(expr=expr):
                with self.assertRaises(BusinessError) as caught:
                    sdk2.execute(compiled(rule(expr)), data)
                self.assertEqual(caught.exception.code, "cardinality")

    def test_empty_fact_batch_evaluates_nothing(self):
        data = {"facts": [], "a": [], "b": []}
        relation = sdk2.execute(compiled(rule(["div", ["lit", 1], ["lit", 0]])), data)
        self.assertEqual(relation.rows, ())
        self.assertEqual(relation.columns, ("position", "value"))

    def test_plan_is_data_independent(self):
        plan = compiled(RULE)
        first = sdk2.execute(plan, tables())
        other = tables()
        other["facts"] = [{"key": 2, "alternate": 2, "amount": 4, "flag": False}]
        other["a"] = [{"key": 2, "value": 11}]
        second = sdk2.execute(plan, other)
        self.assertEqual(sorted(first.records(), key=lambda r: r["position"])[0]["value"], 7)
        self.assertEqual(second.records(), [{"position": 0, "value": Fraction(11, 4)}])


class UpgradeStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "warehouse.sqlite"
        self.db = connect(self.path)
        build_legacy(self.db)
        Runner(self.db).run("daily", "morning")

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_upgrade_moves_rules_and_bindings_and_version(self):
        audit_rules = self.db.execute(
            "SELECT rule_id, ast_json FROM rules_v1 ORDER BY rule_id").fetchall()
        audit_tasks = self.db.execute(
            "SELECT task_id, rule_id FROM tasks_v1 ORDER BY task_id").fetchall()
        upgrade(self.db)
        self.assertEqual(storage_version(self.db), 2)
        self.assertEqual(sdk2.get_plan(self.db, "price")["sdk"], 2)
        self.assertEqual(
            self.db.execute("SELECT plan_id FROM sdk2_tasks ORDER BY task_id").fetchall(),
            [("half",), ("price",)])
        self.assertEqual(Runner(self.db).run("daily", "morning"), EXPECTED)
        # Legacy tables remain, byte-for-byte, for audit only.
        self.assertEqual(
            self.db.execute("SELECT rule_id, ast_json FROM rules_v1 ORDER BY rule_id").fetchall(),
            audit_rules)
        self.assertEqual(
            self.db.execute("SELECT task_id, rule_id FROM tasks_v1 ORDER BY task_id").fetchall(),
            audit_tasks)

    def test_before_commit_failure_rolls_back_and_retry_is_idempotent(self):
        audit = (self.db.execute("SELECT * FROM rules_v1 ORDER BY rule_id").fetchall(),
                 self.db.execute("SELECT * FROM tasks_v1 ORDER BY task_id").fetchall())
        snapshot = list(self.db.iterdump())

        def fail():
            self.assertEqual(storage_version(self.db), 2)
            raise RuntimeError("stop")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            upgrade(self.db, before_commit=fail)
        self.assertEqual(storage_version(self.db), 1)
        self.assertEqual(list(self.db.iterdump()), snapshot)
        self.assertEqual(Runner(self.db).run("daily", "morning"), EXPECTED)
        self.db.close()

        reopened = connect(self.path)
        try:
            self.assertIsNone(upgrade(reopened))
            self.assertEqual(storage_version(reopened), 2)
            dump = list(reopened.iterdump())
            self.assertIsNone(upgrade(reopened, before_commit=lambda: (_ for _ in ()).throw(
                AssertionError("callback must not run"))))
            self.assertEqual(list(reopened.iterdump()), dump)
            self.assertEqual(
                reopened.execute("SELECT * FROM rules_v1 ORDER BY rule_id").fetchall(), audit[0])
        finally:
            reopened.close()

    def test_unsupported_version_rejected(self):
        self.db.execute("UPDATE meta SET value='9' WHERE key='storage_version'")
        with self.assertRaises(ValueError):
            upgrade(self.db)

    def test_run_path_never_uses_sdk1_or_legacy_tables(self):
        import sqlite3
        upgrade(self.db)

        def authorize(operation, table, column, database, trigger):
            if operation == sqlite3.SQLITE_READ and table in {"rules_v1", "tasks_v1"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.db.set_authorizer(authorize)
        try:
            with patch.object(sdk1, "evaluate", side_effect=AssertionError("sdk1 runtime")):
                self.assertEqual(Runner(self.db).run("daily", "morning"), EXPECTED)
                # Native SDK2 replacement is visible through the old object.
                native = {"sdk": 2, "nodes": [
                    {"id": "f", "op": "scan", "source": "facts",
                     "columns": {"amount": "amount"}, "ordinal": "position"},
                    {"id": "p", "op": "project", "input": "f", "fields": {
                        "position": {"col": "position"}, "value": {"col": "amount"}}}],
                    "output": "p"}
                sdk2.save_plan(self.db, "price", native)
                self.assertEqual(Registry(self.db).get("price"), native)
                self.assertEqual(Runner(self.db).run("daily", "morning"),
                                 [10, 10, 3, None, 0])
        finally:
            self.db.set_authorizer(None)

    def test_failed_run_keeps_prior_publication_and_publishes_nothing_new(self):
        upgrade(self.db)
        Registry(self.db).save("price", rule(["lookup", "a", "key"]))
        with self.assertRaises(BusinessError):
            Runner(self.db).run("daily", "morning")
        self.assertEqual(read_publication(self.db, "daily", "morning"), EXPECTED)
        selected = tables()
        selected["facts"][0]["flag"] = False
        put_batch(self.db, "selected", selected)
        with self.assertRaises(BusinessError):
            Runner(self.db).run("daily", "selected")
        self.assertIsNone(read_publication(self.db, "daily", "selected"))


if __name__ == "__main__":
    unittest.main()
