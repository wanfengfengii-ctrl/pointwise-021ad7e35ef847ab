# Saved warehouse rules

Python 3.11, standard library only. Run from the project directory:

- `python3.11 -B -m unittest discover -s tests -v` runs the complete original suite.
- `python3.11 -B demo.py` runs both independent SDK examples in memory.

Tests create file databases only under `tempfile.TemporaryDirectory()`. Production
callers choose their own database path. No network, environment secrets, fixtures
outside this project, or third party packages are used.

## Warehouse API and persistence

`warehouse.connect(path=":memory:")` opens an autocommit SQLite connection with
foreign keys enabled. `samples.build_legacy(db)` creates a complete version-1
database with two rules, two task bindings, and one immutable input batch.
It expects an empty database. `create_legacy(db)` creates only the old schema.

- `Registry(db).save(rule_id, plan)` validates and inserts/replaces an SDK1 rule.
- `Registry(db).get(rule_id)` returns a detached stored executable plan.
  Its `sdk` tag identifies its representation; this adapter stores SDK1 ASTs.
- `Registry(db).bind(task_id, rule_id)` inserts/replaces a task binding.
  Bindings follow the current rule under that ID, rather than freezing a revision.
- `put_batch(db, batch_id, tables)` validates and saves inputs. Repeating the
  same ID and canonical content is idempotent; different content is rejected.
- `Runner(db).run(task_id, batch_id, before_commit=None)` reads a consistent
  rule, binding and batch, computes all rows, then atomically publishes and
  returns the full ordered result list.
- `read_publication(db, task_id, batch_id)` returns the detached last successful
  result, or None when absent. A successful empty result is [].
- A successful rerun replaces that task/batch publication. A failed rerun
  preserves any previous success. No partial result is published.
- Missing rule, task or batch raises KeyError; malformed plans/inputs raise
  ValueError. Business failures raise `common.BusinessError` with `code`
  equal to `division` or `cardinality`.

Schema: `meta(key,value)` contains `storage_version=1`;
`rules_v1(rule_id,ast_json)`; `tasks_v1(task_id,rule_id)`;
`batches(batch_id,payload_json)`; and
`publications(task_id,batch_id,results_json)`. Primary and foreign keys are
defined in `warehouse.create_legacy`; publications have a composite primary key.
The version-1 adapter never reads SDK2 tables.

`transaction(db, before_commit=None)` is a reusable savepoint transaction.
An outer caller transaction owns the final commit when nested. Otherwise
releasing the outermost savepoint commits. The optional callback is invoked
after writes and before release; raising rolls back the whole unit. Ordinary
exceptions, including business failures, roll back. This is a synchronous,
single-connection API; maintenance operations use one worker.

## Fixed data and exact values

Each batch has exactly three list-valued tables: facts, a, b. Every fact has
`key`, `alternate` (nullable integers), `amount` (nullable exact number), and
`flag` (nullable boolean). Every a/b lookup row has `key` (nullable integer)
and `value` (nullable exact number). Extra or missing columns are rejected.
An exact number is an int or fractions.Fraction; bool is not a number.
Floats are not accepted. Row order and duplicate occurrences are meaningful.

`common.dumps/loads` round-trip JSON plus Fractions as
`{"$q":[numerator,denominator]}`. This object shape is reserved for numbers.
The JSON contains no executable Python. All public result and plan objects
are detached from stored text.

## SDK1 rule format and semantics

A plan is `{"sdk":1,"expr":expression}`. Expressions are JSON arrays:

| Expression | Meaning |
| --- | --- |
| ["col", name] | Read a fact column |
| ["lit", value] | Nullable boolean or exact numeric literal |
| ["eq", a, b] | SQL equality; either NULL gives NULL |
| ["div", a, b] | Exact rational division |
| ["lookup", table, key_column] | Scalar value lookup in a or b using fact key or alternate |
| ["case", condition, yes, no] | Evaluate condition, then only the chosen branch |

CASE takes yes only for True; False and NULL take no. Conditions must be
boolean/NULL, and branches must have compatible types. At most two CASE
nodes occur along any expression path, including condition paths; there are
at most 256 expression nodes and depth 32. Lookup arguments cannot contain
another expression or query. This is a fixed rule language, not SQL parsing.

Lookup uses non-NULL key equality. Zero matches gives NULL; one gives its
value (including NULL); more than one raises cardinality even if values are
identical or NULL. NULL keys never match, including other NULL keys.
Division evaluates both operands; either NULL gives NULL, including NULL/0.
Otherwise a zero denominator raises division. Thus CASE is lazy, while a
division operand containing an evaluated bad lookup still raises cardinality.

`sdk1.evaluate(plan, tables)` returns exactly one value per fact occurrence
in original order, retaining duplicate facts independently. An empty fact
table evaluates no expression. If multiple selected expressions fail,
callers may rely on either permitted error code, never a partial list.

The finite sample has hand-calculated output [7,7,2,NULL,6]. The two identical
key=1 facts select constant 7, so their duplicate lookup rows are harmless.
Key=2 yields 6/3; a NULL key yields NULL; key=3 selects b.value/2 = 12/2.
The demo then selects the duplicate lookup and checks that no result publishes.

## SDK2 native port

SDK2 is an independent generic dependency. It has no SDK1 interpreter import,
rule lowering, CASE operator, lazy scalar callback, or warehouse upgrade.
`sdk2.execute(plan, inputs)` takes a data-independent plan and an in-memory
mapping of source name to rows. It returns immutable
`Relation(columns, rows)`; `records()` creates detached dictionaries.

A plan has exactly `sdk=2`, `nodes` (nonempty topological list), and
`output` (node ID). IDs are unique nonempty strings; every input must precede
its consumer. All nodes execute strictly in list order, including nodes
outside the output's ancestor closure. A node error aborts the call.
Intermediate relations are fully materialized; joins use nested loops.
There is no external-memory executor, optimizer, provenance sidecar or cache.

Every node has `id`, `op`, plus these fields:

| op | Fields and behavior |
| --- | --- |
| scan | source, columns={alias:source_column}, optional ordinal=new_column. Ordinal is zero-based source occurrence position, before filtering. |
| filter | input, where=scalar. Keep only True; reject nonboolean/non-NULL predicates. |
| project | input, fields={alias:scalar}. Compute every listed expression for every input row. |
| join | left, right, how="inner" or "left", on=scalar. Input names must be disjoint; predicate follows filter truth rules. Unmatched left rows get NULL right columns. |
| group | input, keys=[column,...], aggregates={alias:{"fn":fn,"column":column_or_null}}. fn is count, sum, min or max; column=null means count(*). |
| union | inputs=[id,...]. UNION ALL aligned by column name, without deduplication. |
| error | input, code="division" or "cardinality". Nonempty input raises that code; empty input produces an empty relation. |

Scalar objects are `{"col":name}`, `{"lit":value}`, or
`{"fn":operator,"args":[...]}`. Operators: add, sub, mul, div, eq, lt, and,
or (binary); not, is_null (unary). Literals/scans allow NULL, bool, int,
Fraction and str. Arithmetic requires exact numbers. Comparisons are for
compatible scalar types. NULL propagates through arithmetic and comparisons;
division uses the SDK1 numeric NULL/zero convention. Boolean operators use
SQL three-valued truth tables, but evaluate every operand first. There is
no scalar CASE, short circuit, user code, source AST, or embedded dataset.

Group keys preserve NULL groups. count(*) counts occurrences; count(column)
ignores NULL. sum/min/max ignore NULL and return NULL for no non-NULL values.
sum is exact and numeric. Empty input with no keys yields one global group;
empty keyed input yields none. Joins, groups and union retain bag semantics.
For compatible typed inputs this reference port emits deterministic rows:
scan/filter/project preserve input order, join is left-major/right-major,
groups appear at first occurrence, union concatenates input lists. Portable
fact ordering should use an explicit scan ordinal. A relation retains its
schema even when empty.

`sdk2.install(db)` installs native target tables
`sdk2_plans(plan_id,dag_json)` and `sdk2_tasks(task_id,plan_id)`, without
changing meta or old tables. Callers can wrap installation and writes in
the provided transaction. `save_plan/get_plan`, `bind_task`, and
`run_saved(db,task_id,inputs)` persist and execute SDK2 plans directly.
These native APIs neither publish warehouse results nor adapt Registry/Runner.
The demo's native half-amount plan is independent of the saved legacy rule.
