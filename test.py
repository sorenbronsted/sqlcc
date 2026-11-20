import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sqlglot as sg
import sqlcc


class SqlCcTest(unittest.TestCase):
    def test_simple(self):
        sql = "select * from table"
        result = sqlcc.calc(sql)
        self.assertEqual(1, result)

    def test_logic(self):
        # 1 + where + 3 and + 3 or + in + between + like + exists + group by
        sql = """
            select * 
            from table 
            where id = 2 
            and id = 3
            or id = 4
            and id in (1,2,3)
            and id between 3 and 4
            or id like '%'
            or exists (select * from b)
            group by id
        """
        result = sqlcc.calc(sql)
        self.assertEqual(13, result)

    def test_where(self):
        self.assertEqual(2, sqlcc.calc("select a from t where a = 1"))
        self.assertEqual(2, sqlcc.calc("delete from t where a = 1"))
        self.assertEqual(2, sqlcc.calc("update t set a = 1 where b = 2"))
        self.assertEqual(3, sqlcc.calc("select * from (select a from t where a = 1) x where x.a = 2"))

    def test_and_or_counted_once(self):
        self.assertEqual(3, sqlcc.calc("select a from t where a = 1 and b = 2"))
        self.assertEqual(3, sqlcc.calc("select a from t where a = 1 or b = 2"))

    def test_like(self):
        self.assertEqual(3, sqlcc.calc("select a from t where a like 'x%'"))
        self.assertEqual(3, sqlcc.calc("select a from t where a ilike 'x%'"))
        self.assertEqual(3, sqlcc.calc("select a from t where a like any ('x%', 'y%')"))

    def test_group(self):
        self.assertEqual(2, sqlcc.calc("select a from t group by a"))

    def test_having_counts_like_where_and_its_content_is_counted_too(self):
        # 1 + group by + having
        self.assertEqual(3, sqlcc.calc("select a from t group by a having a > 1"))
        # 1 + group by + having + between
        self.assertEqual(4, sqlcc.calc("select a from t group by a having count(*) between 1 and 3"))
        # 1 + group by + having + and, which is the same as with between, as count does not branch
        self.assertEqual(4, sqlcc.calc("select a from t group by a having count(*) >= 1 and count(*) <= 3"))

    def test_qualify_counts_like_where(self):
        # 1 + qualify
        self.assertEqual(2, sqlcc.calc("select a from t qualify row_number() over (partition by a order by b) = 1"))
        # 1 + qualify + and
        self.assertEqual(3, sqlcc.calc("select a from t qualify row_number() over (order by b) = 1 and a > 1"))

    def test_a_filter_on_a_window_function_counts_the_same_in_qualify_and_in_a_where(self):
        qualify = "select a from t qualify row_number() over (partition by a order by b) = 1"
        where = "select a from (select a, row_number() over (partition by a order by b) as rn from t) where rn = 1"
        self.assertEqual(sqlcc.calc(qualify), sqlcc.calc(where))

    def test_set_operations(self):
        self.assertEqual(2, sqlcc.calc("select a from t union select a from u"))
        self.assertEqual(2, sqlcc.calc("select a from t union all select a from u"))
        self.assertEqual(2, sqlcc.calc("select a from t intersect select a from u"))
        self.assertEqual(2, sqlcc.calc("select a from t except select a from u"))
        self.assertEqual(3, sqlcc.calc("select a from t union select a from u union all select a from v"))

    def test_case_counts_when_branches(self):
        sql = "select case when a = 1 then 1 when a = 2 then 2 else 3 end from t"
        self.assertEqual(3, sqlcc.calc(sql))
        sql = "select case a when 1 then 'x' when 2 then 'y' when 3 then 'z' end from t"
        self.assertEqual(4, sqlcc.calc(sql))

    def test_functions_which_do_not_branch_do_not_count(self):
        for sql in [
            "select lower(a), upper(b), trim(c) from t",
            "select cast(a as int), a::int from t",
            "select concat(a, b), substring(a, 1, 2), replace(a, 'x', 'y'), md5(a) from t",
            "select count(*), sum(a), min(a), max(a) from t",
            "select row_number() over (partition by a order by b) from t",
            "select rank() over (order by b), sum(a) over (partition by b), first_value(a) over (order by b) from t",
            "select current_timestamp() from t",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(1, sqlcc.calc(sql))

    def test_function_which_sqlglot_does_not_know_does_not_count(self):
        self.assertEqual(1, sqlcc.calc("select my_own_function(a) from t"))

    def test_functions_which_branch_count(self):
        for sql in [
            "select iff(a > 1, 1, 0) from t",
            "select if(a > 1, 1, 0) from t",
            "select coalesce(a, b) from t",
            "select coalesce(a, b, c) from t",
            "select nvl(a, b) from t",
            "select nvl2(a, b, c) from t",
            "select nullif(a, b) from t",
            "select decode(a, 1, 'x', 'y') from t",
            "select greatest(a, b) from t",
            "select least(a, b) from t",
            "select zeroifnull(a) from t",
            "select try_cast(a as int) from t",
            "select count_if(a > 1) from t",
            "select lag(a) over (order by b) from t",
            "select lag(a, 1, 0) over (partition by c order by b) from t",
            "select lead(a) over (order by b) from t",
            "select try_to_number(a) from t",
            "select try_to_decimal(a, 10, 2) from t",
            "select try_to_date(a) from t",
            "select try_to_timestamp(a) from t",
            "select try_to_boolean(a) from t",
            "select try_parse_json(a) from t",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(2, sqlcc.calc(sql))

    def test_functions_which_branch_count_each_time(self):
        self.assertEqual(3, sqlcc.calc("select coalesce(nullif(a, ''), b) from t"))
        self.assertEqual(3, sqlcc.calc("select greatest(a, b), least(a, b) from t"))
        self.assertEqual(3, sqlcc.calc("select lag(a) over (order by b), lead(a) over (order by b) from t"))

    def test_functions_which_are_predicates_count(self):
        for predicate in ["regexp_like(a, 'x')", "startswith(a, 'x')", "endswith(a, 'x')", "contains(a, 'x')",
                          "array_contains(1, arr)", "exists (select 1 from u)"]:
            with self.subTest(predicate=predicate):
                # 1 + where + the predicate
                self.assertEqual(3, sqlcc.calc(f"select a from t where {predicate}"))

    def test_try_and_safe_functions_of_other_dialects_count(self):
        for dialect, sql in [
            ('bigquery', "select safe_divide(a, b) from t"),
            ('bigquery', "select safe_cast(a as int64) from t"),
            ('tsql', "select try_convert(int, a) from t"),
            ('tsql', "select try_parse(a as int) from t"),
            ('databricks', "select try_divide(a, b) from t"),
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(2, sqlcc.calc(sql, dialect=dialect))

    @unittest.skipUnless(hasattr(sg.exp, 'SafeFunc'), "sqlglot only knows SAFE. from a later version")
    def test_safe_prefix_of_bigquery_counts(self):
        self.assertEqual(2, sqlcc.calc("select safe.parse_date('%Y', a) from t", dialect='bigquery'))

    def test_conversions_which_do_not_fail_to_null_do_not_count(self):
        self.assertEqual(1, sqlcc.calc("select to_number(a), to_date(a), parse_json(a), to_boolean(a) from t"))
        self.assertEqual(1, sqlcc.calc("select convert(int, a) from t", dialect='tsql'))

    def test_concat_does_not_count_in_any_dialect(self):
        # sqlglot sets safe on a CONCAT in some dialects, which is about NULL arguments and not about failing
        for dialect in ['snowflake', 'duckdb', 'oracle', 'sqlite', 'mysql', 'postgres', 'bigquery', 'tsql']:
            with self.subTest(dialect=dialect):
                self.assertEqual(1, sqlcc.calc("select concat(a, b), concat_ws(',', a, b) from t", dialect=dialect))
        for dialect in ['snowflake', 'duckdb', 'oracle', 'sqlite', 'postgres']:
            with self.subTest(dialect=dialect, operator='||'):
                self.assertEqual(1, sqlcc.calc("select a || b from t", dialect=dialect))

    def test_predicates_which_are_not_functions_count(self):
        for dialect, predicate in [
            ('snowflake', "a similar to 'x'"),
            ('snowflake', "a > any (select b from u)"),
            ('snowflake', "a > all (select b from u)"),
            ('postgres', "a = any(array[1, 2])"),
            ('postgres', "a && b"),
        ]:
            with self.subTest(predicate=predicate):
                # 1 + where + the predicate
                self.assertEqual(3, sqlcc.calc(f"select a from t where {predicate}", dialect=dialect))

    def test_like_any_counts_as_the_like(self):
        self.assertEqual(3, sqlcc.calc("select a from t where a like any ('x%', 'y%')"))
        self.assertEqual(3, sqlcc.calc("select a from t where a ilike any ('x%', 'y%')"))

    def test_in_of_a_pivot_is_not_a_predicate(self):
        self.assertEqual(1, sqlcc.calc("select * from t pivot(sum(v) for k in ('a', 'b'))"))
        self.assertEqual(1, sqlcc.calc("select * from t unpivot(v for k in (a, b))"))
        # 1 + where + in
        self.assertEqual(3, sqlcc.calc("select * from t pivot(sum(v) for k in ('a', 'b')) where k in (1, 2)"))

    def test_loops_count(self):
        # 1 + recursive + union + where
        sql = "with recursive x(n) as (select 1 union all select n + 1 from x where n < 5) select n from x"
        self.assertEqual(4, sqlcc.calc(sql))
        # 1 + union + where, a CTE which is not recursive adds nothing
        self.assertEqual(3, sqlcc.calc(sql.replace("with recursive", "with")))
        self.assertEqual(1, sqlcc.calc("with x as (select 1 as n) select n from x"))
        # 1 + connect by
        self.assertEqual(2, sqlcc.calc("select a from t start with p is null connect by prior id = p"))

    def test_merge_counts_each_when(self):
        sql = ("merge into t using s on t.id = s.id "
               "when matched then update set a = s.a when not matched then insert (id) values (s.id)")
        self.assertEqual(3, sqlcc.calc(sql))
        sql = ("merge into t using s on t.id = s.id when matched and s.d then delete "
               "when matched then update set a = s.a when not matched then insert (id) values (s.id)")
        self.assertEqual(4, sqlcc.calc(sql))

    def test_multi_table_insert_counts_each_when_but_not_the_else(self):
        self.assertEqual(3, sqlcc.calc(
            "insert all when a > 1 then into x when a <= 1 then into y else into z select a from t"))
        self.assertEqual(1, sqlcc.calc("insert all into x into y select a from t"))

    def test_insert_which_handles_a_conflict_counts(self):
        self.assertEqual(2, sqlcc.calc("insert into t (a) values (1) on conflict (a) do nothing", dialect='postgres'))
        self.assertEqual(2, sqlcc.calc(
            "insert into t (a) values (1) on conflict (a) do update set a = 2", dialect='postgres'))
        self.assertEqual(2, sqlcc.calc(
            "insert into t (a) values (1) on duplicate key update a = 2", dialect='mysql'))

    def test_join(self):
        sql = """
            select * 
            from a
            join b on a.id = b.id 
        """
        result = sqlcc.calc(sql)
        self.assertEqual(2, result)

    def test_join_kinds(self):
        self.assertEqual(2, sqlcc.calc("select * from a left join b on a.id = b.id"))
        self.assertEqual(2, sqlcc.calc("select * from a cross join b"))
        self.assertEqual(2, sqlcc.calc("select * from a, b"))
        self.assertEqual(3, sqlcc.calc("select * from a join b on a.id = b.id join c on a.id = c.id"))

    def test_the_condition_of_a_join_is_counted_by_its_content(self):
        # 1 + join, and the and is the second condition
        self.assertEqual(3, sqlcc.calc("select * from a join b on b.c1 = 'x' and b.c2 = 'y'"))
        self.assertEqual(4, sqlcc.calc("select * from a join b on b.c1 = 'x' and b.c2 = 'y' and b.c3 = 'z'"))

    def test_nested_query(self):
        sql = "with c as (select * from a join b on a.id = b.id) select * from (select * from c) x"
        self.assertEqual(2, sqlcc.calc(sql))

    def test_multiple_statements(self):
        sql = "select a from t where a = 1 and b = 2; select a from t where a = 1 or b = 2;"
        self.assertEqual(5, sqlcc.calc(sql))

    def test_trailing_semicolon(self):
        self.assertEqual(1, sqlcc.calc("select 1;"))

    def test_dialect(self):
        sql = "select `a` from `t` where a = 1 and b = 2"
        self.assertEqual(3, sqlcc.calc(sql, dialect='mysql'))
        with self.assertRaises(sg.errors.ParseError):
            sqlcc.calc(sql)

    def test_unknown_dialect(self):
        with self.assertRaises(ValueError):
            sqlcc.calc("select 1", dialect='nope')

    def test_no_statements(self):
        for sql in ["", "   ", ";", "-- just a comment"]:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    sqlcc.calc(sql)

    def test_not_a_string(self):
        with self.assertRaises(TypeError):
            sqlcc.calc(None)

    def test_invalid_sql(self):
        with self.assertRaises(sg.errors.ParseError):
            sqlcc.calc("selec from")


class SqlCcLocTest(unittest.TestCase):
    def test_every_line_with_code_counts(self):
        self.assertEqual(1, sqlcc.loc("select 1"))
        self.assertEqual(3, sqlcc.loc("select a,\n    b\nfrom t\n"))

    def test_blank_lines_do_not_count(self):
        self.assertEqual(2, sqlcc.loc("select a\n\n   \n\t\nfrom t\n\n"))

    def test_comments_do_not_count(self):
        sql = ("-- head\nselect a, -- trailing\n  b /* inline */\n"
               "/* block\nover lines */\nfrom t\n/* only a comment */\n")
        self.assertEqual(3, sqlcc.loc(sql))

    def test_a_string_over_several_lines_counts_every_line(self):
        self.assertEqual(3, sqlcc.loc("select 'a\nb\nc' as x"))
        self.assertEqual(3, sqlcc.loc("select 'a\n\nc' as x"))

    def test_nothing_but_comments_and_whitespace_is_zero(self):
        for sql in ["", "  \n ", "-- just a comment", "/* a\nblock */", "-- a\n\n/* b */\n"]:
            with self.subTest(sql=sql):
                self.assertEqual(0, sqlcc.loc(sql))

    def test_multiple_statements(self):
        self.assertEqual(2, sqlcc.loc("select 1;\nselect 2;\n"))
        self.assertEqual(1, sqlcc.loc("select 1; select 2;"))

    def test_line_endings(self):
        self.assertEqual(2, sqlcc.loc("select a\r\nfrom t\r\n"))
        self.assertEqual(2, sqlcc.loc("select a\r\n\r\nfrom t"))

    def test_dialect(self):
        self.assertEqual(2, sqlcc.loc("select `a`\nfrom `t`", dialect='mysql'))

    def test_unknown_dialect(self):
        with self.assertRaises(ValueError):
            sqlcc.loc("select 1", dialect='nope')

    def test_not_a_string(self):
        with self.assertRaises(TypeError):
            sqlcc.loc(None)

    def test_sql_which_can_not_be_tokenized(self):
        with self.assertRaises(sg.errors.TokenError):
            sqlcc.loc("select 'abc")


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, relative: str, content: str) -> Path:
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')
        return file

    def manifest(self, relative: str, adapter_type: str) -> Path:
        return self.write(relative, json.dumps({'metadata': {'adapter_type': adapter_type}}))

    def run_cli(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sqlcc.main([str(arg) for arg in args])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class SqlCcCliTest(CliTestCase):
    BACKTICKS = "select `a` from `t` where a = 1 and b = 2"

    def test_scores_directory_recursively_highest_first(self):
        self.write('a.sql', "-- the orders\nselect a\nfrom t\nwhere a = 1 and b = 2\n")
        self.write('b.sql', "select 1")
        self.write('sub/c.sql', "select *\nfrom a\njoin b on a.id = b.id\njoin c on a.id = c.id\n"
                                "join d on a.id = d.id\n")
        code, out, err = self.run_cli(self.root)
        self.assertEqual(0, code)
        rows = [line.split() for line in out.splitlines()]
        self.assertEqual(['4', '3', '1'], [row[0] for row in rows])
        self.assertEqual(['5', '3', '1'], [row[1] for row in rows])
        self.assertEqual(['c.sql', 'a.sql', 'b.sql'], [Path(row[2]).name for row in rows])
        self.assertIn("3 files scored, max 4, average 2.7", err)

    def test_single_file(self):
        file = self.write('a.sql', "select a\nfrom t\nwhere a = 1 and b = 2")
        code, out, err = self.run_cli(file)
        self.assertEqual(0, code)
        self.assertEqual(['3', '3', str(file)], out.split())

    def test_skips_compiled_dbt_tests(self):
        self.write('models/a.sql', "select 1")
        test = self.write('models/schema.yml/not_null_a_id.sql', "select * from a where id is null")
        code, out, err = self.run_cli(self.root / 'models')
        self.assertEqual(0, code)
        self.assertEqual(1, len(out.splitlines()))
        self.assertNotIn('not_null_a_id', out)
        code, out, err = self.run_cli(test)
        self.assertEqual(['2', '1', str(test)], out.split())

    def test_max(self):
        self.write('a.sql', "select a from t where a = 1 and b = 2")
        self.write('c.sql', "select * from a join b on a.id = b.id join c on a.id = c.id join d on a.id = d.id")
        code, out, err = self.run_cli(self.root, '--max', 4)
        self.assertEqual(0, code)
        code, out, err = self.run_cli(self.root, '--max', 3)
        self.assertEqual(1, code)
        self.assertEqual(2, len(out.splitlines()))
        self.assertIn("c.sql: complexity 4 exceeds --max 3", err)
        self.assertNotIn("a.sql: complexity", err)

    def test_file_which_can_not_be_parsed_does_not_stop_the_others(self):
        self.write('a.sql', "select 1")
        self.write('bad.sql', "selec from")
        code, out, err = self.run_cli(self.root)
        self.assertEqual(2, code)
        self.assertEqual(1, len(out.splitlines()))
        self.assertIn("bad.sql: ", err)
        self.assertEqual(1, err.count("bad.sql"))

    def test_file_with_invalid_encoding_is_reported(self):
        (self.root / 'a.sql').write_bytes(b"select '\xff'")
        code, out, err = self.run_cli(self.root)
        self.assertEqual(2, code)
        self.assertIn("a.sql: ", err)

    def test_file_without_statements_is_skipped(self):
        self.write('a.sql', "select 1")
        self.write('empty.sql', "-- nothing here")
        code, out, err = self.run_cli(self.root)
        self.assertEqual(0, code)
        self.assertEqual(1, len(out.splitlines()))
        self.assertIn("empty.sql: skipped", err)

    def test_missing_path(self):
        code, out, err = self.run_cli(self.root / 'nothing')
        self.assertEqual(2, code)
        self.assertIn("does not exist", err)

    def test_directory_without_sql_files(self):
        code, out, err = self.run_cli(self.root)
        self.assertEqual(2, code)
        self.assertIn("no .sql files found", err)

    def test_dialect_without_manifest_is_snowflake(self):
        self.write('a.sql', self.BACKTICKS)
        code, out, err = self.run_cli(self.root)
        self.assertEqual(2, code)
        code, out, err = self.run_cli(self.root, '--dialect', 'mysql')
        self.assertEqual(0, code)
        self.assertEqual('3', out.split()[0])

    def test_dialect_from_manifest_above_the_path(self):
        self.manifest('target/manifest.json', 'mysql')
        self.write('target/compiled/proj/models/a.sql', self.BACKTICKS)
        code, out, err = self.run_cli(self.root / 'target' / 'compiled' / 'proj' / 'models')
        self.assertEqual(0, code)
        self.assertEqual('3', out.split()[0])

    def test_dialect_option_overrides_manifest(self):
        self.manifest('target/manifest.json', 'snowflake')
        self.write('target/compiled/a.sql', self.BACKTICKS)
        code, out, err = self.run_cli(self.root / 'target' / 'compiled')
        self.assertEqual(2, code)
        code, out, err = self.run_cli(self.root / 'target' / 'compiled', '-d', 'mysql')
        self.assertEqual(0, code)

    def test_manifest_option(self):
        manifest = self.manifest('elsewhere/manifest.json', 'mysql')
        self.write('sql/a.sql', self.BACKTICKS)
        code, out, err = self.run_cli(self.root / 'sql', '--manifest', manifest)
        self.assertEqual(0, code)

    def test_adapter_type_named_differently_from_dialect(self):
        self.manifest('target/manifest.json', 'sqlserver')
        self.write('target/compiled/a.sql', "select [a] from [t] where [a] = 1")
        code, out, err = self.run_cli(self.root / 'target' / 'compiled')
        self.assertEqual(0, code)
        self.assertEqual('2', out.split()[0])

    def test_unreadable_manifest(self):
        self.write('target/manifest.json', "not json")
        self.write('target/compiled/a.sql', "select 1")
        code, out, err = self.run_cli(self.root / 'target' / 'compiled')
        self.assertEqual(2, code)
        self.assertIn("use --dialect instead", err)
        code, out, err = self.run_cli(self.root / 'target' / 'compiled', '--manifest', self.root / 'nothing.json')
        self.assertEqual(2, code)

    def test_manifest_without_adapter_type_uses_default_dialect(self):
        self.write('target/manifest.json', "{}")
        self.write('target/compiled/a.sql', "select 1")
        code, out, err = self.run_cli(self.root / 'target' / 'compiled')
        self.assertEqual(0, code)

    def test_unknown_dialect(self):
        self.write('a.sql', "select 1")
        code, out, err = self.run_cli(self.root, '--dialect', 'nope')
        self.assertEqual(2, code)
        self.assertIn("Unknown dialect", err)


class SqlCcCrapTest(unittest.TestCase):
    def test_crap(self):
        for complexity, coverage, expected in [
            (12, 0, 156), (12, 25, 72.75), (12, 50, 30), (12, 75, 14.25), (12, 100, 12), (40, 80, 52.8),
        ]:
            with self.subTest(complexity=complexity, coverage=coverage):
                self.assertAlmostEqual(expected, sqlcc.crap(complexity, coverage))

    def test_crap_is_never_below_the_complexity(self):
        for coverage in range(0, 101, 5):
            self.assertGreaterEqual(sqlcc.crap(30, coverage), 30)

    def test_crap_coverage_out_of_range(self):
        for coverage in [-1, 100.1]:
            with self.subTest(coverage=coverage):
                with self.assertRaises(ValueError):
                    sqlcc.crap(10, coverage)

    def test_coverage_from_tests(self):
        self.assertEqual(0, sqlcc.coverage_from_tests(0, 8))
        self.assertEqual(25, sqlcc.coverage_from_tests(2, 8))
        self.assertEqual(100, sqlcc.coverage_from_tests(8, 8))
        self.assertAlmostEqual(100 / 3, sqlcc.coverage_from_tests(1, 3))

    def test_coverage_from_tests_is_at_most_100(self):
        self.assertEqual(100, sqlcc.coverage_from_tests(20, 8))

    def test_coverage_from_tests_complexity_below_1(self):
        with self.assertRaises(ValueError):
            sqlcc.coverage_from_tests(1, 0)


FCT_ORDERS = 'model.shop.fct_orders'
STG_CUSTOMERS = 'model.shop.stg_customers'
STG_ORDERS = 'model.shop.stg_orders'
# complexity 7: 1 + 2 when + left join + where + in + and, on 3 lines
FCT_ORDERS_SQL = ("select o.id, case when o.amount > 10 then 'big' when o.amount > 2 then 'mid' else 'small' end as size\n"
                  "from stg_orders o left join stg_customers c on o.customer_id = c.id\n"
                  "where o.status in ('paid', 'open') and o.amount > 0")
UNION_SQL = "select 1 as id union all select 2 as id"


def model(sql: str | None, project: str = 'shop', language: str = 'sql') -> dict:
    return {'resource_type': 'model', 'package_name': project, 'language': language, 'compiled_code': sql}


def generic_test(attached: str, *also_depends_on: str, severity: str = 'ERROR') -> dict:
    return {'resource_type': 'test', 'attached_node': attached, 'config': {'severity': severity},
            'depends_on': {'nodes': [attached, *also_depends_on]}}


def singular_test(*depends_on: str) -> dict:
    return {'resource_type': 'test', 'attached_node': None, 'depends_on': {'nodes': list(depends_on)}}


def manifest_of(nodes: dict, adapter_type: str = 'duckdb', **extra) -> dict:
    return {'metadata': {'adapter_type': adapter_type, 'project_name': 'shop'}, 'nodes': nodes, **extra}


def shop_manifest() -> dict:
    """The structure is that of a dbt 1.11 manifest, compiled from a small project."""
    return manifest_of(
        {
            FCT_ORDERS: model(FCT_ORDERS_SQL),
            STG_CUSTOMERS: model(UNION_SQL),
            STG_ORDERS: model(UNION_SQL),
            'test.shop.unique_stg_customers_id': generic_test(STG_CUSTOMERS),
            'test.shop.not_null_stg_customers_id': generic_test(STG_CUSTOMERS),
            'test.shop.row_count_positive_stg_orders': generic_test(STG_ORDERS),
            'test.shop.unique_stg_orders_id': generic_test(STG_ORDERS),
            'test.shop.accepted_values_stg_orders_status': generic_test(STG_ORDERS, severity='warn'),
            'test.shop.relationships_stg_orders_customer_id': generic_test(STG_ORDERS, STG_CUSTOMERS),
            'test.shop.unique_fct_orders_id': generic_test(FCT_ORDERS),
            'test.shop.orders_have_customers': singular_test(STG_ORDERS, STG_CUSTOMERS),
        },
        unit_tests={'unit_test.shop.fct_orders.fct_orders_size': {'depends_on': {'nodes': [FCT_ORDERS]}}},
        disabled={'test.shop.not_null_stg_orders_customer_id': [generic_test(STG_ORDERS)]},
    )


class SqlCcCrapCliTest(CliTestCase):
    BACKTICKS = "select `a` from `t` where a = 1 and b = 2"

    def run_crap(self, manifest: dict, *args) -> tuple[int, str, str]:
        file = self.write('target/manifest.json', json.dumps(manifest))
        return self.run_cli('--crap', '--manifest', file, *args)

    @staticmethod
    def rows(out: str) -> list[list[str]]:
        return [line.split() for line in out.splitlines()[1:]]

    def test_scores_models_highest_crap_first(self):
        code, out, err = self.run_crap(shop_manifest())
        self.assertEqual(0, code)
        self.assertEqual(['CRAP', 'COMP', 'LOC', 'COV%', 'TESTS', 'MISSING', 'MODEL'], out.splitlines()[0].split())
        # fct_orders: 2 tests, a unique and the unit test, so 2 of 7 paths, 28.6%: 49 * (5/7)^3 + 7
        # stg_customers: 3 tests, a unique, a not null and the singular test, relationships belongs to stg_orders
        # stg_orders: 5 tests, with the one with severity warn, but without the disabled one
        self.assertEqual([
            ['24.9', '7', '3', '28.6', '2', '5', FCT_ORDERS],
            ['2.0', '2', '1', '100.0', '3', '0', STG_CUSTOMERS],
            ['2.0', '2', '1', '100.0', '5', '0', STG_ORDERS],
        ], self.rows(out))
        self.assertIn("3 models scored, max CRAP 24.9, 0 without tests", err)

    def test_generic_test_is_only_credited_to_the_attached_model(self):
        a, b = 'model.shop.a', 'model.shop.b'
        manifest = manifest_of({
            a: model("select 1"), b: model("select 1"),
            'test.shop.relationships_a_b': generic_test(a, b),
        })
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)
        self.assertEqual([
            ['2.0', '1', '1', '0.0', '0', '1', b],
            ['1.0', '1', '1', '100.0', '1', '0', a],
        ], self.rows(out))
        self.assertIn("1 without tests", err)

    def test_singular_and_unit_tests_are_credited_to_every_model_they_depend_on(self):
        a, b = 'model.shop.a', 'model.shop.b'
        manifest = manifest_of(
            {a: model("select 1"), b: model("select 1"), 'test.shop.a_and_b': singular_test(a, b)},
            unit_tests={'unit_test.shop.a.a_works': {'depends_on': {'nodes': [a]}}},
        )
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)
        self.assertEqual([
            ['1.0', '1', '1', '100.0', '2', '0', a],
            ['1.0', '1', '1', '100.0', '1', '0', b],
        ], self.rows(out))

    def test_max_crap(self):
        code, out, err = self.run_crap(shop_manifest(), '--max-crap', 24)
        self.assertEqual(1, code)
        self.assertEqual(4, len(out.splitlines()))
        self.assertIn(f"{FCT_ORDERS}: CRAP 24.9 exceeds --max-crap 24", err)
        self.assertNotIn(f"{STG_ORDERS}: CRAP", err)
        code, out, err = self.run_crap(shop_manifest(), '--max-crap', 25)
        self.assertEqual(0, code)

    def test_max_is_the_complexity(self):
        code, out, err = self.run_crap(shop_manifest(), '--max', 6)
        self.assertEqual(1, code)
        self.assertIn(f"{FCT_ORDERS}: complexity 7 exceeds --max 6", err)
        code, out, err = self.run_crap(shop_manifest(), '--max', 7)
        self.assertEqual(0, code)

    def test_options_which_do_not_fit_together(self):
        sql = self.write('a.sql', "select 1")
        for args, message in [
            (['--crap', sql], "PATH can not be used"),
            ([], "PATH, or use --crap"),
            (['--max-crap', 5, sql], "only be used with --crap"),
        ]:
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual(2, code)
                self.assertIn(message, err)

    def test_models_of_other_packages_are_not_scored(self):
        manifest = shop_manifest()
        manifest['nodes']['model.dbt_utils.helper'] = model("select 1", project='dbt_utils')
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)
        self.assertNotIn('dbt_utils', out)
        self.assertIn("3 models scored", err)

    def test_models_which_can_not_be_scored_are_skipped(self):
        manifest = manifest_of({
            'model.shop.ok': model("select 1"),
            'model.shop.py': model("def model(dbt, session): pass", language='python'),
            'model.shop.parsed_only': model(None),
            'model.shop.empty': model("-- nothing"),
        })
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)
        self.assertEqual(1, len(self.rows(out)))
        self.assertIn("model.shop.py: skipped, it is not a SQL model", err)
        self.assertIn("model.shop.parsed_only: skipped, it is not compiled, run dbt compile", err)
        self.assertIn("model.shop.empty: skipped, it contains no SQL statements", err)

    def test_nothing_to_score(self):
        for nodes in [{'model.shop.parsed_only': model(None)}, {}]:
            with self.subTest(nodes=nodes):
                code, out, err = self.run_crap(manifest_of(nodes))
                self.assertEqual(2, code)
                self.assertIn("no models could be scored", err)

    def test_many_skipped_models_are_counted_and_few_are_named(self):
        uncompiled = {f"model.shop.u{i}": model(None) for i in range(5)}
        code, out, err = self.run_crap(manifest_of({'model.shop.ok': model("select 1"), **uncompiled}))
        self.assertEqual(0, code)
        self.assertIn("model.shop.u0: skipped, it is not compiled, run dbt compile", err)
        self.assertNotIn("models skipped", err)

        uncompiled = {f"model.shop.u{i}": model(None) for i in range(6)}
        code, out, err = self.run_crap(manifest_of({'model.shop.ok': model("select 1"), **uncompiled}))
        self.assertEqual(0, code)
        self.assertIn("6 models skipped, it is not compiled, run dbt compile", err)
        self.assertNotIn("model.shop.u0", err)

    def test_nothing_compiled_says_why(self):
        code, out, err = self.run_crap(manifest_of({f"model.shop.u{i}": model(None) for i in range(20)}))
        self.assertEqual(2, code)
        self.assertEqual(
            ["20 models skipped, it is not compiled, run dbt compile", "sqlcc: error: no models could be scored"],
            err.splitlines())

    def test_reader_which_closes_the_pipe_early_is_not_an_error(self):
        nodes = {f"model.shop.m{i}": model("select a from t where a = 1 and b = 2") for i in range(3000)}
        manifest = self.write('big.json', json.dumps(manifest_of(nodes)))
        process = subprocess.Popen([sys.executable, '-B', sqlcc.__file__, '--crap', '--manifest', str(manifest)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        header = process.stdout.readline()
        process.stdout.close()
        err = process.stderr.read()
        process.wait()
        process.stderr.close()
        self.assertIn('CRAP', header)
        self.assertEqual('', err)
        self.assertEqual(141, process.returncode)

    def test_model_which_can_not_be_parsed_does_not_stop_the_others(self):
        code, out, err = self.run_crap(manifest_of({'model.shop.ok': model("select 1"), 'model.shop.bad': model("selec from")}))
        self.assertEqual(2, code)
        self.assertEqual(1, len(self.rows(out)))
        self.assertIn("model.shop.bad: ", err)

    def test_default_manifest_is_in_target(self):
        self.write('target/manifest.json', json.dumps(shop_manifest()))
        with contextlib.chdir(self.root):
            code, out, err = self.run_cli('--crap')
        self.assertEqual(0, code)
        self.assertEqual(3, len(self.rows(out)))

    def test_manifest_which_can_not_be_used(self):
        self.write('not_a_manifest.json', '{"a": 1}')
        self.write('not_json.json', 'not json')
        self.write('a_list.json', '[1, 2]')
        for name, message in [('nothing.json', "can not read"), ('not_json.json', "can not read"),
                              ('a_list.json', "is not a JSON object"), ('not_a_manifest.json', "is not a dbt manifest")]:
            with self.subTest(name=name):
                code, out, err = self.run_cli('--crap', '--manifest', self.root / name)
                self.assertEqual(2, code)
                self.assertIn(message, err)

    def test_dialect_is_the_adapter_type_of_the_manifest(self):
        manifest = manifest_of({'model.shop.a': model(self.BACKTICKS)}, adapter_type='mysql')
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)
        self.assertEqual([['12.0', '3', '1', '0.0', '0', '3', 'model.shop.a']], self.rows(out))

    def test_dialect_option_overrides_the_adapter_type(self):
        manifest = manifest_of({'model.shop.a': model(self.BACKTICKS)}, adapter_type='snowflake')
        code, out, err = self.run_crap(manifest)
        self.assertEqual(2, code)
        code, out, err = self.run_crap(manifest, '--dialect', 'mysql')
        self.assertEqual(0, code)

    def test_adapter_type_named_differently_from_dialect(self):
        manifest = manifest_of({'model.shop.a': model("select [a] from [t]")}, adapter_type='sqlserver')
        code, out, err = self.run_crap(manifest)
        self.assertEqual(0, code)

    def test_unknown_dialect(self):
        code, out, err = self.run_crap(shop_manifest(), '--dialect', 'nope')
        self.assertEqual(2, code)
        self.assertIn("Unknown dialect", err)


if __name__ == '__main__':
    unittest.main()
