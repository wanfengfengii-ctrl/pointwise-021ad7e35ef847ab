"""SDK1: a saved, lazy, single-fact rule interpreter."""
from common import BusinessError, divide, number, tables_copy

COLUMNS = {"key": "number", "alternate": "number", "amount": "number", "flag": "bool"}


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != {"sdk", "expr"} or type(plan["sdk"]) is not int or plan["sdk"] != 1:
        raise ValueError("expected SDK1 rule")
    count = 0

    def visit(expr, cases=0, depth=0):
        nonlocal count
        count += 1
        if count > 256 or depth > 32 or not isinstance(expr, list) or not expr:
            raise ValueError("invalid or oversized expression")
        op = expr[0]
        if op == "col" and len(expr) == 2 and isinstance(expr[1], str) and expr[1] in COLUMNS:
            return COLUMNS[expr[1]]
        if op == "lit" and len(expr) == 2:
            value = expr[1]
            if value is None:
                return "null"
            if type(value) is bool:
                return "bool"
            if number(value):
                return "number"
            raise ValueError("invalid literal")
        if op == "lookup" and len(expr) == 3:
            if expr[1] not in ("a", "b") or expr[2] not in ("key", "alternate"):
                raise ValueError("lookup requires a table and fact key column")
            return "number"
        if op in ("div", "eq") and len(expr) == 3:
            left = visit(expr[1], cases, depth + 1)
            right = visit(expr[2], cases, depth + 1)
            if op == "div":
                if left not in ("number", "null") or right not in ("number", "null"):
                    raise ValueError("division requires numbers")
                return "number"
            if left != right and "null" not in (left, right):
                raise ValueError("equality type mismatch")
            return "bool"
        if op == "case" and len(expr) == 4 and cases < 2:
            if visit(expr[1], cases + 1, depth + 1) not in ("bool", "null"):
                raise ValueError("CASE requires boolean")
            yes = visit(expr[2], cases + 1, depth + 1)
            no = visit(expr[3], cases + 1, depth + 1)
            if yes != no and "null" not in (yes, no):
                raise ValueError("CASE branch type mismatch")
            return no if yes == "null" else yes
        raise ValueError("unsupported SDK1 expression")

    visit(plan["expr"])


def evaluate(plan, tables):
    validate(plan)
    tables = tables_copy(tables)

    def value(expr, fact):
        op = expr[0]
        if op == "col":
            return fact[expr[1]]
        if op == "lit":
            return expr[1]
        if op == "case":
            condition = value(expr[1], fact)
            return value(expr[2] if condition is True else expr[3], fact)
        if op == "lookup":
            key = fact[expr[2]]
            matches = [] if key is None else [r["value"] for r in tables[expr[1]]
                                             if r["key"] is not None and r["key"] == key]
            if len(matches) > 1:
                raise BusinessError("cardinality")
            return matches[0] if matches else None
        left = value(expr[1], fact)
        right = value(expr[2], fact)
        if op == "eq":
            return None if left is None or right is None else left == right
        return divide(left, right)

    return [value(plan["expr"], fact) for fact in tables["facts"]]
