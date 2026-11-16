"""SDK2: serializable, strict, materialized relational DAGs.

No SDK1 AST, callbacks, rule compiler, or branch/error compatibility layer.
"""
from dataclasses import dataclass

from common import BusinessError, divide, dumps, loads, number, scalar

ARITY = {"add": 2, "sub": 2, "mul": 2, "div": 2, "eq": 2, "lt": 2,
         "and": 2, "or": 2, "not": 1, "is_null": 1}


@dataclass(frozen=True)
class Relation:
    columns: tuple
    rows: tuple

    def records(self):
        return [dict(zip(self.columns, row)) for row in self.rows]


def _expression(expr, columns, depth=0):
    if depth > 32 or not isinstance(expr, dict):
        raise ValueError("invalid scalar expression")
    if set(expr) == {"col"}:
        if not isinstance(expr["col"], str) or expr["col"] not in columns:
            raise ValueError("unknown column")
    elif set(expr) == {"lit"}:
        if not scalar(expr["lit"]):
            raise ValueError("invalid scalar literal")
    elif set(expr) == {"fn", "args"}:
        fn, args = expr["fn"], expr["args"]
        if not isinstance(fn, str) or fn not in ARITY or not isinstance(args, list) or len(args) != ARITY[fn]:
            raise ValueError("unknown scalar operator or arity")
        for arg in args:
            _expression(arg, columns, depth + 1)
    else:
        raise ValueError("unsupported scalar expression")


def _truth(value):
    if value is not None and type(value) is not bool:
        raise ValueError("boolean or null required")
    return value


def _value(expr, row):
    if "col" in expr:
        return row[expr["col"]]
    if "lit" in expr:
        return expr["lit"]
    fn = expr["fn"]
    args = [_value(arg, row) for arg in expr["args"]]
    a = args[0]
    if fn == "is_null":
        return a is None
    if fn == "not":
        return None if _truth(a) is None else not a
    b = args[1]
    if fn in ("and", "or"):
        _truth(a)
        _truth(b)
        if fn == "and":
            return False if a is False or b is False else (None if a is None or b is None else True)
        return True if a is True or b is True else (None if a is None or b is None else False)
    if fn == "div":
        return divide(a, b)
    if a is None or b is None:
        return None
    if fn == "eq":
        return a == b
    if fn == "lt":
        return a < b
    if not number(a) or not number(b):
        raise ValueError("numeric operands required")
    if fn == "add":
        return a + b
    if fn == "sub":
        return a - b
    return a * b


def _names(values):
    return (isinstance(values, list) and all(isinstance(x, str) and x for x in values)
            and len(set(values)) == len(values))


def _mapping(value):
    return isinstance(value, dict) and all(isinstance(x, str) and x for x in value)


def validate(plan):
    if (not isinstance(plan, dict) or set(plan) != {"sdk", "nodes", "output"}
            or type(plan["sdk"]) is not int or plan["sdk"] != 2
            or not isinstance(plan["nodes"], list) or not plan["nodes"]):
        raise ValueError("expected SDK2 DAG")
    schemas = {}

    def source(node, field):
        name = node[field]
        if not isinstance(name, str) or name not in schemas:
            raise ValueError("inputs must precede consumers")
        return schemas[name]

    fields = {
        "scan": ({"source", "columns"}, {"ordinal"}),
        "filter": ({"input", "where"}, set()),
        "project": ({"input", "fields"}, set()),
        "join": ({"left", "right", "how", "on"}, set()),
        "group": ({"input", "keys", "aggregates"}, set()),
        "union": ({"inputs"}, set()),
        "error": ({"input", "code"}, set())
    }
    for node in plan["nodes"]:
        if not isinstance(node, dict):
            raise ValueError("invalid node")
        name, op = node.get("id"), node.get("op")
        if not isinstance(name, str) or not name or name in schemas or not isinstance(op, str) or op not in fields:
            raise ValueError("invalid node identity or operator")
        required, optional = fields[op]
        actual = set(node) - {"id", "op"}
        if not required <= actual or actual - required - optional:
            raise ValueError("invalid node fields")
        if op == "scan":
            mapping = node["columns"]
            if (not isinstance(node["source"], str) or not node["source"]
                    or not _mapping(mapping)
                    or not all(isinstance(x, str) and x for x in mapping.values())):
                raise ValueError("invalid scan")
            cols = sorted(mapping)
            if "ordinal" in node:
                ordinal = node["ordinal"]
                if not isinstance(ordinal, str) or not ordinal or ordinal in cols:
                    raise ValueError("invalid ordinal column")
                cols.append(ordinal)
        elif op in ("filter", "project", "error"):
            cols = source(node, "input")
            if op == "filter":
                _expression(node["where"], cols)
            elif op == "project":
                if not _mapping(node["fields"]):
                    raise ValueError("invalid projection")
                for expr in node["fields"].values():
                    _expression(expr, cols)
                cols = sorted(node["fields"])
            elif node["code"] not in ("division", "cardinality"):
                raise ValueError("invalid terminal error")
        elif op == "join":
            left, right = source(node, "left"), source(node, "right")
            if set(left) & set(right) or node["how"] not in ("inner", "left"):
                raise ValueError("join requires disjoint names and supported kind")
            cols = tuple(left) + tuple(right)
            _expression(node["on"], cols)
        elif op == "group":
            incoming = source(node, "input")
            keys, aggregates = node["keys"], node["aggregates"]
            if not _names(keys) or not set(keys) <= set(incoming) or not _mapping(aggregates) or set(keys) & set(aggregates):
                raise ValueError("invalid group outputs")
            for spec in aggregates.values():
                if not isinstance(spec, dict) or set(spec) != {"fn", "column"}:
                    raise ValueError("invalid aggregate")
                if spec["fn"] not in ("count", "sum", "min", "max"):
                    raise ValueError("unsupported aggregate")
                column = spec["column"]
                if column is None:
                    if spec["fn"] != "count":
                        raise ValueError("only count accepts star")
                elif not isinstance(column, str) or column not in incoming:
                    raise ValueError("unknown aggregate column")
            cols = keys + sorted(aggregates)
        else:
            inputs = node["inputs"]
            if not isinstance(inputs, list) or not inputs:
                raise ValueError("union requires inputs")
            cols = source({"input": inputs[0]}, "input")
            for ref in inputs:
                if set(source({"input": ref}, "input")) != set(cols):
                    raise ValueError("union schema mismatch")
        schemas[name] = tuple(cols)
    if not isinstance(plan["output"], str) or plan["output"] not in schemas:
        raise ValueError("unknown output")
    return schemas


def execute(plan, inputs):
    schemas = validate(plan)
    relations = {}
    for node in plan["nodes"]:
        name, op = node["id"], node["op"]
        cols = schemas[name]
        if op == "scan":
            records = []
            for ordinal, row in enumerate(inputs[node["source"]]):
                record = {}
                for alias, column in node["columns"].items():
                    item = row[column]
                    if not scalar(item):
                        raise ValueError("scan supports scalar values only")
                    record[alias] = item
                if "ordinal" in node:
                    record[node["ordinal"]] = ordinal
                records.append(record)
        elif op == "filter":
            records = [row for row in relations[node["input"]].records()
                       if _truth(_value(node["where"], row)) is True]
        elif op == "project":
            records = [{alias: _value(expr, row) for alias, expr in node["fields"].items()}
                       for row in relations[node["input"]].records()]
        elif op == "join":
            records = []
            right = relations[node["right"]]
            for left in relations[node["left"]].records():
                matched = False
                for row in right.records():
                    combined = dict(left, **row)
                    if _truth(_value(node["on"], combined)) is True:
                        matched = True
                        records.append(combined)
                if not matched and node["how"] == "left":
                    records.append(dict(left, **dict.fromkeys(right.columns)))
        elif op == "union":
            records = [row for ref in node["inputs"] for row in relations[ref].records()]
        elif op == "error":
            incoming = relations[node["input"]]
            if incoming.rows:
                raise BusinessError(node["code"])
            records = []
        else:
            groups = {}
            for row in relations[node["input"]].records():
                key = tuple(row[column] for column in node["keys"])
                groups.setdefault(key, []).append(row)
            if not node["keys"] and not groups:
                groups[()] = []
            records = []
            for key, members in groups.items():
                row = dict(zip(node["keys"], key))
                for alias, spec in node["aggregates"].items():
                    column, fn = spec["column"], spec["fn"]
                    values = [r[column] for r in members if r[column] is not None] if column is not None else []
                    if fn == "count":
                        row[alias] = len(members) if column is None else len(values)
                    elif not values:
                        row[alias] = None
                    elif fn == "sum":
                        if not all(number(x) for x in values):
                            raise ValueError("sum requires numbers")
                        row[alias] = sum(values)
                    elif fn == "min":
                        row[alias] = min(values)
                    else:
                        row[alias] = max(values)
                records.append(row)
        relations[name] = Relation(cols, tuple(tuple(row[column] for column in cols) for row in records))
    return relations[plan["output"]]


def install(db):
    # Target SDK's native tables. This does not change the warehouse storage version.
    db.execute("CREATE TABLE IF NOT EXISTS sdk2_plans(plan_id TEXT PRIMARY KEY, dag_json TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS sdk2_tasks(
        task_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES sdk2_plans(plan_id))""")


def save_plan(db, plan_id, plan):
    if not isinstance(plan_id, str) or not plan_id:
        raise ValueError("plan id required")
    validate(plan)
    db.execute("""INSERT INTO sdk2_plans VALUES(?,?)
        ON CONFLICT(plan_id) DO UPDATE SET dag_json=excluded.dag_json""", (plan_id, dumps(plan)))


def get_plan(db, plan_id):
    row = db.execute("SELECT dag_json FROM sdk2_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise KeyError(plan_id)
    return loads(row[0])


def bind_task(db, task_id, plan_id):
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id required")
    get_plan(db, plan_id)
    db.execute("""INSERT INTO sdk2_tasks VALUES(?,?)
        ON CONFLICT(task_id) DO UPDATE SET plan_id=excluded.plan_id""", (task_id, plan_id))


def run_saved(db, task_id, inputs):
    row = db.execute("SELECT plan_id FROM sdk2_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    return execute(get_plan(db, row[0]), inputs)
