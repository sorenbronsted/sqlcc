# sqlcc
A [cyclomatic complexity](https://en.wikipedia.org/wiki/Cyclomatic_complexity) 
calculator for SQL

## Installation

```
pip install sqlccc
```
The package is named `sqlccc` (three c's), but the module you import is `sqlcc`.

## Usage

```python
import sqlcc

cc = sqlcc.calc('select a, b from foo')
cc = sqlcc.calc('select `a` from `foo`', dialect='mysql')
```
which returns a number representing the cyclomatic complexity of the SQL.
The SQL is parsed with the [sqlglot](https://github.com/tobymao/sqlglot) dialect given by `dialect`,
which defaults to `snowflake`.

The SQL can contain several statements, in which case the complexity of all of them is summed.

`calc` raises
* `TypeError` if the SQL is not a string
* `ValueError` if the SQL contains no statements, or the dialect is unknown
* `sqlglot.errors.ParseError` if the SQL can not be parsed

`crap(complexity, coverage)` returns the [CRAP1 score](https://testing.googleblog.com/2011/02/this-code-is-crap.html),
`complexity^2 * (1 - coverage/100)^3 + complexity`, where the coverage is in percent. It is never lower than the
complexity. `coverage_from_tests(tests, complexity)` estimates the coverage from a number of tests, see
[CRAP for dbt models](#crap-for-dbt-models).

## Command line

```
sqlcc target/compiled/my_project/models
```
prints the complexity of every `.sql` file found in the directories (or of the files) given, the highest first:
```
     8  target/compiled/my_project/models/marts/fct_orders.sql
     2  target/compiled/my_project/models/staging/stg_orders.sql
```
followed, on stderr, by a summary. `python -m sqlcc` does the same.

| Option             | Effect                                                                                                |
|--------------------|-------------------------------------------------------------------------------------------------------|
| `-d`, `--dialect`  | The sqlglot dialect, see below for the default                                                        |
| `--manifest FILE`  | A dbt `manifest.json` to find the dialect in. With `--crap` also where the models and tests are found |
| `--max N`          | Exit with status 1 if a file, or with `--crap` a model, has a complexity above `N`, and name it       |
| `--crap`           | Score the models in the manifest by CRAP instead of the files, see below                              |
| `--max-crap N`     | With `--crap`, exit with status 1 if a model has a CRAP score above `N`, and name it                  |

The exit status is 0 when everything is scored and nothing exceeds `--max` or `--max-crap`, 1 when something exceeds them,
and 2 when something can not be parsed, or the arguments are invalid. A file without SQL statements is skipped with a note.

### With dbt

sqlcc can not parse the Jinja in dbt models, so run it on the SQL that dbt compiles:

```
dbt compile
sqlcc target/compiled/my_project/models --max 50
```
* The dialect is the `adapter_type` of the `manifest.json` in, or above, the given path, which for the path above is
  `target/manifest.json`. It is mapped to the sqlglot dialect of the same name, except `sqlserver`, which is `tsql`.
  Without a manifest it is `snowflake`. `--dialect` always wins.
* dbt compiles the tests of a `schema.yml` into a directory named `schema.yml`. These are skipped when a directory is
  searched, but not when you name such a file. The singular tests are compiled to `tests` next to `models` and are not
  skipped, so name the `models` directory and not the project.

## CRAP for dbt models

`--crap` ranks the models of a dbt project by their [CRAP score](https://testing.googleblog.com/2011/02/this-code-is-crap.html),
which is the complexity of the model weighted by how little it is tested. Compile the project into a folder of its own,
and point sqlcc at the manifest there:

```
dbt compile --target-path /tmp/dbt_sqlcc
sqlcc --crap --manifest /tmp/dbt_sqlcc/manifest.json --max-crap 100
```
It prints the models with the highest score first:
```
     CRAP   COMP    COV%  TESTS  MISSING  MODEL
     24.9      7    28.6      2        5  model.shop.fct_orders
      2.0      2   100.0      3        0  model.shop.stg_customers
      2.0      2   100.0      5        0  model.shop.stg_orders
```
`COMP` is the complexity of the compiled SQL, `COV%` the estimated coverage, `TESTS` the number of tests and `MISSING`
the number of tests it takes to get to 100%. `CRAP` is `COMP^2 * (1 - COV/100)^3 + COMP`.

**The coverage is an estimate.** CRAP is defined on the basis path coverage of the automated tests, but a dbt test
asserts something about the data and does not run the paths of the SQL. The complexity is the number of tests it takes
to cover every basis path, so each test is assumed to cover one path: `COV% = min(100, TESTS / COMP * 100)`. Ten
`not_null` tests count as ten paths, so the number says something about how much a model is tested, not how well.

The tests are counted as follows:
* A generic test, e.g. `unique`, `relationships` or one of your own, counts for the model it is attached to. A
  `relationships` test also depends on the model it refers to, but that model gets nothing.
* A singular test, a `.sql` file in `tests`, counts in full for every model it depends on through `ref()`. A table
  named directly in the SQL is not seen.
* A unit test counts for the model it tests.
* A test with `severity: warn` counts. A disabled test does not, because it is not in the manifest.

Only the definitions in the manifest are used, not whether the tests passed, and a test of a model downstream does not
count for the models it reads.

Only the models of the project which made the manifest are scored, not the ones in packages. The SQL is the
`compiled_code` in the manifest. A Python model, a model which is not compiled, and a model without SQL statements are
skipped. A few are named on stderr, and more are counted, e.g. `1363 models skipped, it is not compiled, run dbt compile`.
Nothing scored at all is an error, so a manifest from `dbt parse` does not pass a check by being empty.

**Every dbt command writes `manifest.json`, and only the models which that command compiled have a `compiled_code`.**
After `dbt run -s some_model` the manifest has the SQL of `some_model` and its tests and nothing else, and while it
runs, or after `dbt parse` or `dbt ls`, it has none. Without a `--manifest`, sqlcc reads `target/manifest.json`, which
the next dbt command overwrites. That is why the example above compiles into its own `--target-path`, and why a
`dbt compile --select` gives a score for the selected models only.

The CRAP score is never lower than the complexity, and the lack of tests weighs more the more complex the model is:

| Coverage | CRAP at complexity 12 | CRAP at complexity 40 |
|----------|-----------------------|-----------------------|
| 0%       | 156                   | 1640                  |
| 50%      | 30                    | 240                   |
| 80%      | 13.2                  | 52.8                  |
| 100%     | 12                    | 40                    |

The limit of 30 which is known from CRAP was found for the complexity of Java methods. The complexity here only counts
the functions which branch, so choose a `--max-crap` for your own project.

## How the complexity is calculated

The complexity starts at 1 and each of the following adds to it:

| Construct                                                                                        | Adds                       |
|--------------------------------------------------------------------------------------------------|----------------------------|
| `WHERE`, `HAVING`, `QUALIFY`                                                                     | 1 per clause               |
| `AND`, `OR`                                                                                      | 1 each                     |
| `IN`, `BETWEEN`, `LIKE`, `ILIKE`, `SIMILAR TO`, `ANY`, `ALL`                                     | 1 each                     |
| `GROUP BY`                                                                                       | 1                          |
| `WHEN` branch of a `CASE`, and `IF`, `IFF` (the `CASE` itself adds nothing)                      | 1 per branch or call       |
| `COALESCE`, `NVL`, `IFNULL`, `NULLIF`, `NVL2`, `DECODE`, `GREATEST`, `LEAST`, `EXISTS`           | 1 per call                 |
| `LAG`, `LEAD`                                                                                    | 1 per call                 |
| `TRY_CAST`, `SAFE_CAST`, `SAFE_DIVIDE`, `TRY_TO_NUMBER`, `TRY_CONVERT` and other `TRY_`/`SAFE_`  | 1 per call                 |
| `REGEXP_LIKE`, `RLIKE`, `CONTAINS`, `STARTSWITH`, `ENDSWITH`, `ARRAY_CONTAINS`, `ARRAYS_OVERLAP` | 1 per call                 |
| `COUNT_IF`                                                                                       | 1 per call                 |
| `JOIN` of any kind, including a comma join                                                       | 1 per join                 |
| `UNION`, `INTERSECT`, `EXCEPT`                                                                   | 1 per operator             |
| `WITH RECURSIVE`, `CONNECT BY`                                                                   | 1 each                     |
| `WHEN` of a `MERGE` or multi table `INSERT`, `ON CONFLICT`, `ON DUPLICATE KEY`                   | 1 each                     |

Everything is counted anywhere in the SQL, including subqueries and CTEs. A construct is only counted once.

A join counts 1, like `WHERE` and `GROUP BY`: the rows which match go on, and the others are left out, or become NULL
for an outer join. The condition of the join is counted by what it contains, so `JOIN b ON b.c1 = 'x' AND b.c2 = 'y'`
adds 1 for the join and 1 for the `AND`.

**A function counts when it branches**, which means that the path through the SQL depends on the data, and a test is
needed for each path. `COALESCE` has a path for a NULL and one for a value. `CAST`, `LOWER`, `TRIM`, `CONCAT` and
`SUBSTRING` do the same whatever the data is, and so they add nothing. Neither do the aggregates (`COUNT`, `SUM`,
`MAX`, ...), as they change the grain and do not branch, and `GROUP BY` counts on its own.
`TRY_CAST` counts, because a value which can not be converted gives NULL, which is a path of its own. So do the other
functions which give NULL instead of an error, like `TRY_TO_NUMBER`, `TRY_CONVERT`, `TRY_PARSE_JSON` and `SAFE_DIVIDE`,
also when sqlglot does not know them but their name starts with `TRY_` or `SAFE_`. Any other function which sqlglot does
not know adds nothing, as nothing can be said about it.

**A clause which filters rows counts**, and `WHERE`, `HAVING` and `QUALIFY` do. The expression of the filter can have
any length, so it is also counted by what it contains, like `AND` and `BETWEEN`. That is why
`HAVING COUNT(*) BETWEEN 1 AND 3` and `HAVING COUNT(*) >= 1 AND COUNT(*) <= 3` get the same complexity.

**A window function does not filter and does not branch**, except `LAG` and `LEAD`: the first or last row of a
partition has no neighbour, which gives NULL or the default, so they count 1 per call. `ROW_NUMBER`, `RANK`, `SUM() OVER`
and the like add nothing, and a filter on them is counted by the filter. `QUALIFY ROW_NUMBER() OVER (...) = 1` and
`SELECT ... FROM (SELECT ROW_NUMBER() OVER (...) AS rn ...) WHERE rn = 1` therefore get the same complexity.

**A loop counts**: a recursive CTE (`WITH RECURSIVE`) and `CONNECT BY` repeat until nothing is left, and count 1 each, in
addition to the `UNION` and the `WHERE` they have.

**A statement which branches counts**: each `WHEN` of a `MERGE` (`MATCHED`, `NOT MATCHED`) and of a multi table
`INSERT ALL WHEN` counts 1, but the `ELSE` of the `INSERT` does not, like the `ELSE` of a `CASE`. `ON CONFLICT` and
`ON DUPLICATE KEY` count 1. The `IN` of a `PIVOT` lists the columns and is not a predicate, so it does not count, and
`LIKE ANY (...)` counts as the `LIKE` and not as an `ANY` too.

Not counted (yet): comparisons, also `IS NULL` and `IS DISTINCT FROM`, subqueries and CTEs.
The weights are defined in `WEIGHTS` in `sqlcc.py`. `COALESCE` counts 1 however many arguments it has.

Which functions sqlglot knows depends on its version, and an unknown function adds nothing. For example, `IFF` in the
Databricks dialect is unknown to sqlglot 30.0 but known to 30.5, and the same SQL can therefore get a different
complexity.

## Development

```
python3 -m venv .venv-linux
.venv-linux/bin/pip install -r requirements-dev.txt
make test            # runs the tests
make test-coverage   # runs the tests and prints which lines of the code they cover, using coverage.py
make coverage        # prints the cyclomatic complexity of the Python code, using radon
```
`make` uses `.venv-linux/bin/python` when it exists, and else `python3`. Use `make test PYTHON=path/to/python` to run
with another interpreter.
