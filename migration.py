"""Atomic one-worker storage migration from SDK1 rules to SDK2 DAGs."""
from common import dumps, loads
import sdk1
import sdk2
import warehouse
from compiler import compile_plan
from warehouse import storage_version, transaction

META_VERSION = "storage_version"


def _convert_rules(db):
    rows = db.execute("SELECT rule_id, ast_json FROM rules_v1 ORDER BY rowid").fetchall()
    converted = []
    for rule_id, ast_json in rows:
        rule = loads(ast_json)
        sdk1.validate(rule)  # legacy validator guards the compiler input
        plan = compile_plan(rule)
        sdk2.validate(plan)  # the unmodified target SDK must accept the DAG
        converted.append((rule_id, plan))
    return converted


def upgrade(db, *, before_commit=None):
    """Migrate storage version 1 to 2 inside one savepoint transaction.

    Version 2 is a no-op (without invoking any callback). Any other storage
    version raises ValueError. A raised before_commit (or any other failure)
    rolls back every migration write, leaving the version-1 database usable
    and the upgrade idempotently retryable after reopening.
    """
    version = storage_version(db)
    if version == 2:
        return None
    if version != 1:
        raise ValueError("unsupported storage version: %r" % version)
    with transaction(db, before_commit):
        sdk2.install(db)
        for rule_id, plan in _convert_rules(db):
            # Original rule_id becomes the native plan_id; detached DAG text.
            sdk2.save_plan(db, rule_id, plan)
        bindings = db.execute(
            "SELECT task_id, rule_id FROM tasks_v1 ORDER BY rowid").fetchall()
        for task_id, rule_id in bindings:
            # Task ids and current bindings are carried over unchanged.
            sdk2.bind_task(db, task_id, rule_id)
        db.execute("UPDATE meta SET value='2' WHERE key=?", (META_VERSION,))
    return None
