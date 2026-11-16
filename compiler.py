"""Pure SDK1 expression -> SDK2 relational DAG compiler.

The compiler reads only a saved SDK1 AST. It never reads batches, never embeds
input data, and contains no interpreter or row callback: its output is a
data-independent SDK2 plan accepted by the unmodified ``sdk2.validate`` and
executable by ``sdk2.execute`` / ``sdk2.run_saved`` on later batches.

Representation outline
----------------------
* One ``scan`` of source ``facts`` carries a zero-based ``position`` per fact
  occurrence, so duplicate facts stay distinct and the Runner can restore the
  original order.
* A "branch base" always carries the full fact columns plus ``position`` for
  the currently live fact occurrences. Every compiled sub-expression instead
  returns a narrow ``(position, value)`` relation with one row per live
  position. CASE filters narrow the live set, then an inner join back to the
  fact scan restores fact columns only for the selected positions, so an
  unselected branch is never materialized (lazy semantics).
* A ``lookup`` is a LEFT JOIN on non-NULL equality followed by a GROUP on
  ``position``. Zero matches survive as NULL (the unmatched left row); more
  than one match routes the offending positions through an ``error`` node
  tagged ``cardinality``. Each lookup occurrence gets its own join chain.
* Division is a projected ``div`` scalar: SDK2 evaluates both operands first
  and reuses the SDK1 NULL/zero convention, so NULL operands cannot mask an
  error already raised by an evaluated lookup, while NULL/0 yields NULL.
"""

FACT_COLUMNS = ("key", "alternate", "amount", "flag")
POSITION = "position"


class Compiler:
    def __init__(self):
        self.nodes = []
        self.counter = 0
        self.lookup_scans = {}
        self.fact_scan = None

    def _new_id(self, prefix):
        node_id = "%s%d" % (prefix, self.counter)
        self.counter += 1
        return node_id

    def _add(self, node):
        self.nodes.append(node)
        return node["id"]

    def _fact_scan_id(self):
        if self.fact_scan is None:
            self.fact_scan = self._add({
                "id": self._new_id("facts_scan_"), "op": "scan",
                "source": "facts",
                "columns": {column: column for column in FACT_COLUMNS},
                "ordinal": POSITION})
        return self.fact_scan

    def _lookup_scan(self, table):
        if table not in self.lookup_scans:
            self.lookup_scans[table] = self._add({
                "id": self._new_id("scan_%s_" % table), "op": "scan",
                "source": table,
                "columns": {"lk_%s" % table: "key", "lv_%s" % table: "value"}})
        return self.lookup_scans[table]

    def _project(self, prefix, input_id, fields):
        return self._add({"id": self._new_id(prefix), "op": "project",
                          "input": input_id, "fields": fields})

    def _restore_facts(self, prefix, live_id):
        """Re-attach full fact columns for a filtered set of live positions."""
        pivot = self._project(prefix + "pivot_", live_id, {"p": {"col": POSITION}})
        scan_id = self._fact_scan_id()
        join_id = self._add({
            "id": self._new_id(prefix + "join_"), "op": "join",
            "left": pivot, "right": scan_id, "how": "inner",
            "on": {"fn": "eq", "args": [{"col": "p"}, {"col": POSITION}]}})
        fields = {POSITION: {"col": POSITION}}
        for column in FACT_COLUMNS:
            fields[column] = {"col": column}
        return self._project(prefix + "base_", join_id, fields)

    def compile(self, expr, base):
        """Return (node_id, value_alias); base holds fact columns+position."""
        op = expr[0]
        if op == "lit":
            alias = self._new_id("v_")
            node_id = self._project("lit_", base, {
                POSITION: {"col": POSITION}, alias: {"lit": expr[1]}})
            return node_id, alias
        if op == "col":
            alias = self._new_id("v_")
            node_id = self._project("col_", base, {
                POSITION: {"col": POSITION}, alias: {"col": expr[1]}})
            return node_id, alias
        if op in ("eq", "div"):
            left_id, left_alias = self.compile(expr[1], base)
            right_id, right_alias = self.compile(expr[2], base)
            lp, rp = self._new_id("p_"), self._new_id("p_")
            lv, rv = self._new_id("side_"), self._new_id("side_")
            left_id = self._project("ren_", left_id, {lp: {"col": POSITION}, lv: {"col": left_alias}})
            right_id = self._project("ren_", right_id, {rp: {"col": POSITION}, rv: {"col": right_alias}})
            join_id = self._add({
                "id": self._new_id("join_"), "op": "join",
                "left": left_id, "right": right_id, "how": "inner",
                "on": {"fn": "eq", "args": [{"col": lp}, {"col": rp}]}})
            if op == "eq":
                scalar = {"fn": "eq", "args": [{"col": lv}, {"col": rv}]}
            else:
                scalar = {"fn": "div", "args": [{"col": lv}, {"col": rv}]}
            alias = self._new_id("v_")
            node_id = self._project("bin_", join_id, {
                POSITION: {"col": lp}, alias: scalar})
            return node_id, alias
        if op == "lookup":
            table, key_column = expr[1], expr[2]
            scan_id = self._lookup_scan(table)
            key_alias = self._new_id("fk_")
            key_id = self._project("key_", base, {
                POSITION: {"col": POSITION}, key_alias: {"col": key_column}})
            lk, lv = "lk_%s" % table, "lv_%s" % table
            join_id = self._add({
                "id": self._new_id("join_"), "op": "join",
                "left": key_id, "right": scan_id, "how": "left",
                # NULL on either side makes equality NULL, so NULL fact keys
                # and NULL lookup keys never match; unmatched facts stay NULL.
                "on": {"fn": "eq", "args": [{"col": key_alias}, {"col": lk}]}})
            count_alias, group_value = self._new_id("cnt_"), self._new_id("gv_")
            group_id = self._add({
                "id": self._new_id("group_"), "op": "group", "input": join_id,
                "keys": [POSITION],
                "aggregates": {
                    # count(*) counts matched occurrences, including NULL
                    # values, so duplicate NULL rows still trigger cardinality.
                    count_alias: {"fn": "count", "column": None},
                    group_value: {"fn": "min", "column": lv}}})
            union_alias = self._new_id("u_")
            bad_id = self._add({
                "id": self._new_id("filter_"), "op": "filter", "input": group_id,
                "where": {"fn": "lt", "args": [{"lit": 1}, {"col": count_alias}]}})
            # Align the schema before the terminal error so the error node can
            # itself be a union input (reachable, executed in node order).
            bad_id = self._project("badname_", bad_id, {
                POSITION: {"col": POSITION}, union_alias: {"lit": None}})
            error_id = self._add({
                "id": self._new_id("error_"), "op": "error",
                "input": bad_id, "code": "cardinality"})
            # The good side keeps zero/one-match positions only; bad positions
            # live exclusively on the error side, so positions never double.
            good_filter = self._add({
                "id": self._new_id("filter_"), "op": "filter", "input": group_id,
                "where": {"fn": "or", "args": [
                    {"fn": "eq", "args": [{"col": count_alias}, {"lit": 0}]},
                    {"fn": "eq", "args": [{"col": count_alias}, {"lit": 1}]}]}})
            good_id = self._project("goodname_", good_filter, {
                POSITION: {"col": POSITION}, union_alias: {"col": group_value}})
            # An empty error relation keeps its schema, so the union aligns;
            # a nonempty one raises cardinality before any result is read.
            union_id = self._add({
                "id": self._new_id("union_"), "op": "union",
                "inputs": [good_id, error_id]})
            return union_id, union_alias
        if op == "case":
            cond_id, cond_alias = self.compile(expr[1], base)
            yes_live = self._add({
                "id": self._new_id("sel_"), "op": "filter", "input": cond_id,
                "where": {"fn": "eq", "args": [{"col": cond_alias}, {"lit": True}]}})
            no_live = self._add({
                "id": self._new_id("sel_"), "op": "filter", "input": cond_id,
                # False and NULL both take the no branch. A filter keeps only
                # True, so combine an explicit NULL test with negation.
                "where": {"fn": "or", "args": [
                    {"fn": "is_null", "args": [
                        {"fn": "eq", "args": [{"col": cond_alias}, {"lit": True}]}]},
                    {"fn": "not", "args": [
                        {"fn": "eq", "args": [{"col": cond_alias}, {"lit": True}]}]}]}})
            yes_base = self._restore_facts("yes_", yes_live)
            no_base = self._restore_facts("no_", no_live)
            yes_id, yes_alias = self.compile(expr[2], yes_base)
            no_id, no_alias = self.compile(expr[3], no_base)
            branch_alias = self._new_id("branch_")
            yes_id = self._project("branchname_", yes_id, {
                POSITION: {"col": POSITION}, branch_alias: {"col": yes_alias}})
            no_id = self._project("branchname_", no_id, {
                POSITION: {"col": POSITION}, branch_alias: {"col": no_alias}})
            union_id = self._add({
                "id": self._new_id("union_"), "op": "union",
                "inputs": [yes_id, no_id]})
            return union_id, branch_alias
        raise ValueError("unsupported SDK1 expression")


def compile_plan(rule):
    """Compile a validated SDK1 rule into an executable SDK2 DAG."""
    compiler = Compiler()
    base = compiler._restore_facts("root_", compiler._project(
        "rootscan_", compiler._fact_scan_id(), {POSITION: {"col": POSITION}}))
    root_id, root_alias = compiler.compile(rule["expr"], base)
    output_id = compiler._project("output_", root_id, {
        POSITION: {"col": POSITION}, "value": {"col": root_alias}})
    return {"sdk": 2, "nodes": compiler.nodes, "output": output_id}
