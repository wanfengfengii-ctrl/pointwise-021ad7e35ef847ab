import copy
import unittest
from fractions import Fraction

import sdk2
from common import BusinessError, dumps, loads
from warehouse import connect


def col(name):
    return {"col": name}


def lit(value):
    return {"lit": value}


def call(fn, *args):
    return {"fn": fn, "args": list(args)}


def graph(nodes, output=None):
    return {"sdk": 2, "nodes": nodes, "output": output or nodes[-1]["id"]}


def scan(name="s", source="data", columns=None, **kwargs):
    return {"id": name, "op": "scan", "source": source,
            "columns": {"x": "x"} if columns is None else columns, **kwargs}


class SDK2PortTests(unittest.TestCase):
    def run_graph(self, plan, inputs):
        # Round-tripping every port fixture exercises the saved wire format.
        return sdk2.execute(loads(dumps(plan)), inputs).records()

    def test_scan_ordinal_is_per_occurrence(self):
        plan = graph([scan(ordinal="position")])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": 8}, {"x": 8}, {"x": None}]}),
                         [{"x": 8, "position": 0}, {"x": 8, "position": 1}, {"x": None, "position": 2}])

    def test_project_filter_exact_values(self):
        plan = graph([scan(), {"id": "f", "op": "filter", "input": "s",
                               "where": call("lt", lit(0), col("x"))},
                      {"id": "p", "op": "project", "input": "f",
                       "fields": {"third": call("div", col("x"), lit(3))}}])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": -1}, {"x": None}, {"x": 2}]}),
                         [{"third": Fraction(2, 3)}])

    def test_three_valued_boolean_operators(self):
        for a in (True, False, None):
            for b in (True, False, None):
                plan = graph([scan(), {"id": "p", "op": "project", "input": "s", "fields": {
                    "both": call("and", lit(a), lit(b)),
                    "either": call("or", lit(a), lit(b)),
                    "negated": call("not", lit(a)),
                    "null": call("is_null", lit(a))}}])
                expected_and = False if False in (a, b) else (None if None in (a, b) else True)
                expected_or = True if True in (a, b) else (None if None in (a, b) else False)
                self.assertEqual(self.run_graph(plan, {"data": [{"x": 0}]}),
                                 [{"both": expected_and, "either": expected_or,
                                   "negated": None if a is None else not a, "null": a is None}])

    def test_strict_boolean_does_not_short_circuit(self):
        plan = graph([scan(), {"id": "p", "op": "project", "input": "s", "fields": {
            "x": call("and", lit(False), call("eq", call("div", lit(1), lit(0)), lit(0)))}}])
        with self.assertRaises(BusinessError) as caught:
            self.run_graph(plan, {"data": [{"x": 1}]})
        self.assertEqual(caught.exception.code, "division")

    def test_all_nodes_execute_even_if_output_is_earlier(self):
        plan = graph([scan(), {"id": "bad", "op": "project", "input": "s",
                              "fields": {"x": call("div", lit(1), lit(0))}}], output="s")
        with self.assertRaises(BusinessError):
            self.run_graph(plan, {"data": [{"x": 3}]})

    def test_filter_limits_downstream_operand_rows(self):
        plan = graph([scan(), {"id": "nonzero", "op": "filter", "input": "s",
                               "where": call("not", call("eq", col("x"), lit(0)))},
                      {"id": "p", "op": "project", "input": "nonzero",
                       "fields": {"v": call("div", lit(1), col("x"))}}])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": 0}, {"x": 2}, {"x": None}]}),
                         [{"v": Fraction(1, 2)}])

    def test_left_join_bag_semantics_and_nulls(self):
        nodes = [scan("l", "left", {"lk": "k", "v": "v"}),
                 scan("r", "right", {"rk": "k", "w": "w"}),
                 {"id": "j", "op": "join", "left": "l", "right": "r", "how": "left",
                  "on": call("eq", col("lk"), col("rk"))}]
        inputs = {"left": [{"k": 1, "v": 3}, {"k": 1, "v": 3}, {"k": None, "v": 5}, {"k": 2, "v": 7}],
                  "right": [{"k": 1, "w": 8}, {"k": 1, "w": 9}, {"k": None, "w": 10}]}
        expected = [{"lk": 1, "v": 3, "rk": 1, "w": w} for _ in range(2) for w in (8, 9)]
        expected += [{"lk": None, "v": 5, "rk": None, "w": None},
                     {"lk": 2, "v": 7, "rk": None, "w": None}]
        self.assertEqual(self.run_graph(graph(nodes), inputs), expected)
        nodes[-1]["how"] = "inner"
        self.assertEqual(self.run_graph(graph(nodes), inputs), expected[:4])

    def test_empty_right_join_preserves_left(self):
        plan = graph([scan("l", "left", {"x": "x"}), scan("r", "right", {"y": "x"}),
                      {"id": "j", "op": "join", "left": "l", "right": "r", "how": "left",
                       "on": call("eq", col("x"), col("y"))}])
        self.assertEqual(self.run_graph(plan, {"left": [{"x": 4}], "right": []}),
                         [{"x": 4, "y": None}])

    def test_group_counts_nulls_and_exact_sum(self):
        plan = graph([scan(columns={"k": "k", "x": "x"}),
                      {"id": "g", "op": "group", "input": "s", "keys": ["k"], "aggregates": {
                          "rows": {"fn": "count", "column": None},
                          "values": {"fn": "count", "column": "x"},
                          "total": {"fn": "sum", "column": "x"},
                          "low": {"fn": "min", "column": "x"},
                          "high": {"fn": "max", "column": "x"}}}])
        data = [{"k": 1, "x": Fraction(1, 3)}, {"k": 1, "x": None},
                {"k": 1, "x": Fraction(2, 3)}, {"k": None, "x": None}]
        self.assertEqual(self.run_graph(plan, {"data": data}),
                         [{"k": 1, "rows": 3, "values": 2, "total": 1,
                           "low": Fraction(1, 3), "high": Fraction(2, 3)},
                          {"k": None, "rows": 1, "values": 0, "total": None, "low": None, "high": None}])

    def test_empty_global_and_grouped_aggregate(self):
        plan = graph([scan(), {"id": "g", "op": "group", "input": "s", "keys": [], "aggregates": {
            "n": {"fn": "count", "column": None}, "s": {"fn": "sum", "column": "x"}}}])
        self.assertEqual(self.run_graph(plan, {"data": []}), [{"n": 0, "s": None}])
        plan["nodes"][1]["keys"] = ["x"]
        self.assertEqual(self.run_graph(plan, {"data": []}), [])

    def test_union_keeps_duplicates_and_shared_inputs(self):
        plan = graph([scan(), {"id": "u", "op": "union", "inputs": ["s", "s"]}])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": 5}, {"x": 5}]}), [{"x": 5}] * 4)

    def test_group_output_can_feed_terminal_error(self):
        plan = graph([scan(), {"id": "g", "op": "group", "input": "s", "keys": ["x"],
                               "aggregates": {"n": {"fn": "count", "column": None}}},
                      {"id": "f", "op": "filter", "input": "g", "where": call("lt", lit(1), col("n"))},
                      {"id": "e", "op": "error", "input": "f", "code": "cardinality"}])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": 1}, {"x": 2}]}), [])
        with self.assertRaises(BusinessError) as caught:
            self.run_graph(plan, {"data": [{"x": 1}, {"x": 1}]})
        self.assertEqual(caught.exception.code, "cardinality")

    def test_other_arithmetic_and_null_propagation(self):
        plan = graph([scan(), {"id": "p", "op": "project", "input": "s", "fields": {
            "v": call("mul", call("sub", call("add", col("x"), lit(2)), lit(1)), lit(3)),
            "eq": call("eq", col("x"), lit(None))}}])
        self.assertEqual(self.run_graph(plan, {"data": [{"x": 4}, {"x": None}]}),
                         [{"v": 15, "eq": None}, {"v": None, "eq": None}])

    def test_native_persistence_and_data_independence(self):
        db = connect()
        try:
            sdk2.install(db)
            plan = graph([scan(ordinal="row"), {"id": "p", "op": "project", "input": "s",
                                              "fields": {"v": call("div", col("x"), lit(2)), "row": col("row")}}])
            sdk2.save_plan(db, "native", plan)
            sdk2.bind_task(db, "task", "native")
            plan["nodes"].clear()
            self.assertEqual(sdk2.run_saved(db, "task", {"data": [{"x": 3}, {"x": 3}]}).records(),
                             [{"v": Fraction(3, 2), "row": 0}, {"v": Fraction(3, 2), "row": 1}])
            self.assertEqual(sdk2.run_saved(db, "task", {"data": [{"x": 7}]}).records(),
                             [{"v": Fraction(7, 2), "row": 0}])
            self.assertEqual(db.execute("SELECT count(*) FROM sdk2_tasks").fetchone()[0], 1)
        finally:
            db.close()

    def test_invalid_graphs_rejected_without_callbacks(self):
        valid = graph([scan()])
        invalid = []
        bad = copy.deepcopy(valid)
        bad["nodes"].append({"id": "p", "op": "project", "input": "s", "fields": {"x": {"col": "missing"}}})
        invalid.append(bad)
        bad = copy.deepcopy(valid)
        bad["nodes"].append({"id": "j", "op": "join", "left": "s", "right": "s",
                             "how": "left", "on": lit(True)})
        invalid.append(bad)
        invalid += [graph([scan(), scan()]),
                    graph([{"id": "f", "op": "filter", "input": "future", "where": lit(True)}]),
                    {"sdk": 1, "nodes": [scan()], "output": "s"},
                    graph([{"id": "p", "op": "python", "callback": "sdk1.evaluate"}]),
                    graph([scan(), {"id": "u", "op": "union", "inputs": []}])]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                sdk2.validate(plan)


if __name__ == "__main__":
    unittest.main()
