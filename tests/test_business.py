import copy
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import sdk1
from common import BusinessError, dumps, loads
from samples import EXPECTED, RULE, build_legacy, tables
from warehouse import (Registry, Runner, connect, put_batch, read_publication,
                       storage_version, transaction)


def rule(expr):
    return {"sdk": 1, "expr": expr}


def dataset(facts, a=(), b=()):
    return {"facts": facts, "a": list(a), "b": list(b)}


def fact(key=1, amount=10, flag=True, alternate=2):
    return {"key": key, "amount": amount, "flag": flag, "alternate": alternate}


class SemanticsTests(unittest.TestCase):
    def assert_code(self, code, plan, data):
        with self.assertRaises(BusinessError) as caught:
            sdk1.evaluate(plan, data)
        self.assertEqual(caught.exception.code, code)

    def test_hand_calculated_sample(self):
        self.assertEqual(sdk1.evaluate(RULE, tables()), EXPECTED)

    def test_branch_exception_scope(self):
        bad = [["div", ["lit", 1], ["lit", 0]], ["lookup", "a", "key"]]
        data = dataset([fact()], [{"key": 1, "value": 2}, {"key": 1, "value": 3}])
        for expression, code in zip(bad, ("division", "cardinality")):
            for condition in (True, False, None):
                for bad_on_true in (True, False):
                    yes, no = (expression, ["lit", 7]) if bad_on_true else (["lit", 7], expression)
                    plan = rule(["case", ["lit", condition], yes, no])
                    selected_bad = (condition is True) == bad_on_true
                    with self.subTest(code=code, condition=condition, bad_on_true=bad_on_true):
                        if selected_bad:
                            self.assert_code(code, plan, data)
                        else:
                            self.assertEqual(sdk1.evaluate(plan, data), [7])

    def test_scalar_lookup_zero_one_many_and_null(self):
        plan = rule(["lookup", "a", "key"])
        rows = [fact(key=None), fact(key=2), fact(key=3), fact(key=3)]
        data = dataset(rows, [{"key": None, "value": 91}, {"key": None, "value": 92},
                              {"key": 3, "value": Fraction(5, 2)}])
        self.assertEqual(sdk1.evaluate(plan, data), [None, None, Fraction(5, 2), Fraction(5, 2)])
        data["a"].append({"key": 3, "value": None})
        self.assert_code("cardinality", plan, data)

    def test_alternate_key_and_both_tables(self):
        plan = rule(["div", ["lookup", "a", "alternate"], ["lookup", "b", "key"]])
        data = dataset([fact()], [{"key": 2, "value": 9}], [{"key": 1, "value": 6}])
        self.assertEqual(sdk1.evaluate(plan, data), [Fraction(3, 2)])

    def test_null_operand_does_not_hide_evaluated_lookup_error(self):
        data = dataset([fact()], [{"key": 1, "value": None}, {"key": 1, "value": None}])
        for left, right in ((["lit", None], ["lookup", "a", "key"]),
                            (["lookup", "a", "key"], ["lit", None])):
            self.assert_code("cardinality", rule(["div", left, right]), data)

    def test_condition_errors_are_selected_work(self):
        plan = rule(["case", ["eq", ["lookup", "a", "key"], ["lit", 1]],
                     ["lit", 7], ["lit", 8]])
        data = dataset([fact()], [{"key": 1, "value": 1}, {"key": 1, "value": 1}])
        self.assert_code("cardinality", plan, data)

    def test_exact_division_and_null(self):
        for left, right, expected in [(1, 3, Fraction(1, 3)),
                                      (Fraction(-5, 7), Fraction(3, 11), Fraction(-55, 21)),
                                      (None, 0, None), (1, None, None), (None, None, None)]:
            with self.subTest(left=left, right=right):
                self.assertEqual(sdk1.evaluate(rule(["div", ["lit", left], ["lit", right]]),
                                               dataset([fact()])), [expected])
        self.assert_code("division", rule(["div", ["lit", 0], ["lit", 0]]), dataset([fact()]))

    def test_nested_case_and_unknown_condition(self):
        plan = rule(["case", ["eq", ["col", "key"], ["lit", None]],
                     ["div", ["lit", 1], ["lit", 0]],
                     ["case", ["col", "flag"], ["lit", Fraction(2, 3)], ["lit", 5]]])
        self.assertEqual(sdk1.evaluate(plan, dataset([fact(flag=True), fact(flag=None)])),
                         [Fraction(2, 3), 5])

    def test_empty_input_does_not_evaluate_expression(self):
        self.assertEqual(sdk1.evaluate(rule(["div", ["lit", 1], ["lit", 0]]), dataset([])), [])

    def test_language_and_input_validation(self):
        bad = [
            rule(["case", ["lit", 1], ["lit", 2], ["lit", 3]]),
            rule(["div", ["col", "flag"], ["lit", 1]]),
            rule(["lookup", "c", "key"]),
            rule(["lookup", "a", ["col", "key"]]),
            rule(["lit", 1.5]),
            rule(["case", ["lit", True], ["lit", 1],
                  ["case", ["lit", True], ["lit", 2],
                   ["case", ["lit", True], ["lit", 3], ["lit", 4]]]])
        ]
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                sdk1.validate(plan)
        with self.assertRaises(ValueError):
            sdk1.evaluate(rule(["lit", 1]), dataset([fact(key=True)]))

    def test_serialization_has_no_float_rounding(self):
        value = {"q": Fraction(2**150 + 1, 3**40), "n": None, "b": False}
        self.assertEqual(loads(dumps(value)), value)


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        build_legacy(self.db)
        self.registry = Registry(self.db)
        self.runner = Runner(self.db)

    def tearDown(self):
        self.db.close()

    def test_old_schema_and_multiple_bindings(self):
        self.assertEqual(storage_version(self.db), 1)
        self.assertEqual(self.registry.get("price"), RULE)
        self.assertEqual(self.runner.run("daily", "morning"), EXPECTED)
        self.assertEqual(self.runner.run("audit", "morning"), [5, 5, Fraction(3, 2), None, 0])
        self.assertEqual(read_publication(self.db, "daily", "morning"), EXPECTED)
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks_v1").fetchone()[0], 2)

    def test_plan_uses_subsequent_batch(self):
        self.runner.run("daily", "morning")
        later = tables()
        later["facts"] = [fact(key=2, amount=4, flag=False)]
        later["a"] = [{"key": 2, "value": 11}]
        put_batch(self.db, "evening", later)
        self.assertEqual(self.runner.run("daily", "evening"), [Fraction(11, 4)])
        self.assertEqual(read_publication(self.db, "daily", "morning"), EXPECTED)

    def test_registry_and_input_are_detached(self):
        plan = copy.deepcopy(RULE)
        self.registry.save("copy", plan)
        plan["expr"] = ["lit", 99]
        fetched = self.registry.get("copy")
        fetched["expr"] = ["lit", -1]
        self.assertEqual(self.registry.get("copy"), RULE)
        source = tables()
        put_batch(self.db, "copy", source)
        source["facts"].clear()
        self.assertEqual(self.runner.run("daily", "copy"), EXPECTED)

    def test_rebind_and_replace_are_live(self):
        self.registry.bind("daily", "half")
        self.assertEqual(self.runner.run("daily", "morning"), [5, 5, Fraction(3, 2), None, 0])
        self.registry.save("half", rule(["lit", 4]))
        self.assertEqual(self.runner.run("daily", "morning"), [4] * 5)
        self.assertEqual(self.runner.run("audit", "morning"), [4] * 5)

    def test_error_never_publishes_partial_rows(self):
        self.registry.save("fault", rule(["div", ["lit", 1], ["col", "amount"]]))
        self.registry.bind("fault-task", "fault")
        put_batch(self.db, "fault-batch", dataset([fact(amount=2), fact(amount=0)]))
        with self.assertRaises(BusinessError) as caught:
            self.runner.run("fault-task", "fault-batch")
        self.assertEqual(caught.exception.code, "division")
        self.assertIsNone(read_publication(self.db, "fault-task", "fault-batch"))
        self.assertEqual(self.runner.run("daily", "morning"), EXPECTED)

    def test_failed_replacement_keeps_previous_publication(self):
        self.runner.run("daily", "morning")
        self.registry.save("price", rule(["lookup", "a", "key"]))
        with self.assertRaises(BusinessError):
            self.runner.run("daily", "morning")
        self.assertEqual(read_publication(self.db, "daily", "morning"), EXPECTED)

    def test_before_commit_failure_rolls_back_and_retry_works(self):
        def fail():
            raise RuntimeError("commit interrupted")
        with self.assertRaisesRegex(RuntimeError, "commit interrupted"):
            self.runner.run("daily", "morning", before_commit=fail)
        self.assertIsNone(read_publication(self.db, "daily", "morning"))
        self.assertEqual(self.runner.run("daily", "morning"), EXPECTED)

    def test_batch_identity_and_missing_ids(self):
        put_batch(self.db, "morning", tables())
        modified = tables()
        modified["facts"].reverse()
        with self.assertRaises(ValueError):
            put_batch(self.db, "morning", modified)
        with self.assertRaises(KeyError):
            self.registry.get("missing")
        with self.assertRaises(KeyError):
            self.registry.bind("orphan", "missing")
        with self.assertRaises(KeyError):
            self.runner.run("missing", "morning")
        with self.assertRaises(KeyError):
            self.runner.run("daily", "missing")

    def test_nested_transaction_owns_final_commit(self):
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            with transaction(self.db):
                self.registry.save("new", rule(["lit", 6]))
                self.registry.bind("new-task", "new")
                self.runner.run("new-task", "morning")
                raise RuntimeError("cancel")
        with self.assertRaises(KeyError):
            self.registry.get("new")
        self.assertIsNone(read_publication(self.db, "new-task", "morning"))

    def test_invalid_save_preserves_original(self):
        with self.assertRaises(ValueError):
            self.registry.save("price", rule(["lit", "not a numeric literal"]))
        self.assertEqual(self.registry.get("price"), RULE)

    def test_reopen_saved_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.sqlite"
            db = connect(path)
            build_legacy(db)
            Runner(db).run("daily", "morning")
            db.close()
            reopened = connect(path)
            try:
                self.assertEqual(Registry(reopened).get("price"), RULE)
                self.assertEqual(Runner(reopened).run("daily", "morning"), EXPECTED)
                self.assertEqual(read_publication(reopened, "daily", "morning"), EXPECTED)
            finally:
                reopened.close()

    def test_reopen_rollback_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.sqlite"
            db = connect(path)
            build_legacy(db)
            def fail():
                raise OSError("publication interrupted")
            with self.assertRaises(OSError):
                Runner(db).run("daily", "morning", before_commit=fail)
            db.close()
            reopened = connect(path)
            try:
                self.assertIsNone(read_publication(reopened, "daily", "morning"))
                self.assertEqual(Runner(reopened).run("daily", "morning"), EXPECTED)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
