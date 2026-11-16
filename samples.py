"""Finite legacy data, including duplicates and unselected bad lookups."""
from fractions import Fraction

from warehouse import Registry, create_legacy, put_batch

RULE = {"sdk": 1, "expr": [
    "case", ["col", "flag"], ["lit", 7],
    ["case", ["eq", ["col", "key"], ["lit", 3]],
     ["div", ["lookup", "b", "key"], ["lit", 2]],
     ["div", ["lookup", "a", "key"], ["col", "amount"]]]
]}

EXPECTED = [Fraction(7), Fraction(7), Fraction(2), None, Fraction(6)]


def tables():
    first = {"key": 1, "alternate": 9, "amount": 10, "flag": True}
    return {
        "facts": [dict(first), dict(first),
                  {"key": 2, "alternate": 2, "amount": 3, "flag": False},
                  {"key": None, "alternate": None, "amount": None, "flag": None},
                  {"key": 3, "alternate": 3, "amount": 0, "flag": False}],
        "a": [{"key": 1, "value": 99}, {"key": 1, "value": 100},
              {"key": 2, "value": 6}, {"key": None, "value": 50}],
        "b": [{"key": 3, "value": 12}]
    }


def build_legacy(db):
    create_legacy(db)
    registry = Registry(db)
    registry.save("price", RULE)
    registry.bind("daily", "price")
    registry.save("half", {"sdk": 1, "expr": ["div", ["col", "amount"], ["lit", 2]]})
    registry.bind("audit", "half")
    put_batch(db, "morning", tables())
