"""Deterministic CLI demonstration; all state is in memory."""
import copy

import sdk2
from common import BusinessError, dumps
from samples import EXPECTED, build_legacy, tables
from warehouse import Runner, connect, put_batch, read_publication


def main():
    db = connect()
    try:
        build_legacy(db)
        result = Runner(db).run("daily", "morning")
        if result != EXPECTED:
            raise AssertionError("legacy sample differs from hand calculation")
        selected = copy.deepcopy(tables())
        selected["facts"] = [dict(selected["facts"][0], flag=False)]
        put_batch(db, "selected", selected)
        try:
            Runner(db).run("daily", "selected")
        except BusinessError as error:
            code = error.code
        else:
            raise AssertionError("selected duplicate lookup must fail")
        if code != "cardinality" or read_publication(db, "daily", "selected") is not None:
            raise AssertionError("incorrect failure publication")

        # A separate native SDK2 plan, not a translation of the saved legacy rule.
        sdk2.install(db)
        plan = {"sdk": 2, "nodes": [
            {"id": "facts", "op": "scan", "source": "facts",
             "columns": {"amount": "amount"}, "ordinal": "position"},
            {"id": "halves", "op": "project", "input": "facts", "fields": {
                "position": {"col": "position"},
                "half": {"fn": "div", "args": [{"col": "amount"}, {"lit": 2}]}}}
        ], "output": "halves"}
        sdk2.save_plan(db, "native-half", plan)
        sdk2.bind_task(db, "native-audit", "native-half")
        native = sdk2.run_saved(db, "native-audit", tables()).records()
        print(dumps({"legacy_results": result, "selected_error": code,
                     "failed_batch_published": False, "native_sdk2_results": native}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
