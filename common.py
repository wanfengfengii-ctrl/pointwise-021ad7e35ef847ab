"""Exact values and the fixed warehouse input schema."""
import json
from fractions import Fraction


class BusinessError(Exception):
    def __init__(self, code):
        if code not in {"division", "cardinality"}:
            raise ValueError("unknown business error")
        self.code = code
        super().__init__(code)


def dumps(value):
    def encode(item):
        if isinstance(item, Fraction):
            return {"$q": [item.numerator, item.denominator]}
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=encode, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def loads(text):
    def decode(item):
        if set(item) == {"$q"}:
            pair = item["$q"]
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(x) is not int for x in pair) or pair[1] == 0):
                raise ValueError("invalid rational")
            return Fraction(*pair)
        return item
    return json.loads(text, object_hook=decode)


def scalar(value):
    return value is None or type(value) in {bool, int, str, Fraction}


def number(value):
    return type(value) in {int, Fraction}


def divide(left, right):
    if left is None or right is None:
        return None
    if not number(left) or not number(right):
        raise ValueError("numeric operands required")
    if right == 0:
        raise BusinessError("division")
    return Fraction(left) / Fraction(right)


def tables_copy(tables):
    if not isinstance(tables, dict) or set(tables) != {"facts", "a", "b"}:
        raise ValueError("expected facts, a, b")
    result = {}
    for name, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError("table must be a list")
        columns = {"key", "alternate", "amount", "flag"} if name == "facts" else {"key", "value"}
        checked = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != columns:
                raise ValueError("invalid row columns")
            if row["key"] is not None and type(row["key"]) is not int:
                raise ValueError("key must be nullable integer")
            if name == "facts":
                if row["alternate"] is not None and type(row["alternate"]) is not int:
                    raise ValueError("alternate must be nullable integer")
                if row["flag"] is not None and type(row["flag"]) is not bool:
                    raise ValueError("flag must be nullable boolean")
                val = row["amount"]
            else:
                val = row["value"]
            if val is not None and not number(val):
                raise ValueError("measure must be exact numeric or null")
            checked.append(dict(row))
        result[name] = checked
    return result
