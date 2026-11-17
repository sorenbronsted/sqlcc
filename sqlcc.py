import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import sqlglot as sg

DEFAULT_DIALECT = 'snowflake'
DEFAULT_MANIFEST = Path('target') / 'manifest.json'
MAX_SKIPPED_LISTED = 5

# dbt adapter types that are named differently from the sqlglot dialect
ADAPTER_DIALECTS = {'sqlserver': 'tsql'}

# The weight each construct adds to the complexity. A node gets the weight of the most specific class
# in its inheritance chain, so a Union counts as the SetOperation it is, and a node which has no class
# in WEIGHTS adds nothing.
WEIGHTS = {
    # Logic: HAVING is not weighted, its expression varies in length and is counted by what it contains
    sg.exp.Where: 1, sg.exp.And: 1, sg.exp.Or: 1, sg.exp.In: 1, sg.exp.Between: 1,
    sg.exp.Like: 1, sg.exp.ILike: 1, sg.exp.Group: 1,
    # Functions: only the ones which branch count, and a CASE through its WHEN branches (sg.exp.If). The others,
    # like CAST and LOWER, take the same path whatever the data is. So do the ones sqlglot does not know
    # (sg.exp.Anonymous), as nothing can be said about them.
    sg.exp.If: 1, sg.exp.Coalesce: 1, sg.exp.Nullif: 1, sg.exp.Nvl2: 1, sg.exp.DecodeCase: 1,
    sg.exp.Greatest: 1, sg.exp.Least: 1, sg.exp.Exists: 1,
    # A conversion which fails gives NULL, and that is a path of its own
    sg.exp.TryCast: 1,
    # Functions which are predicates, like LIKE and IN
    sg.exp.RegexpLike: 1, sg.exp.RegexpILike: 1, sg.exp.Contains: 1, sg.exp.StartsWith: 1,
    sg.exp.EndsWith: 1, sg.exp.ArrayContains: 1, sg.exp.CountIf: 1,
    # Sets: SetOperation is UNION, INTERSECT and EXCEPT
    sg.exp.Join: 1, sg.exp.SetOperation: 1,
}


def _weight(node: sg.exp.Expression) -> int:
    for cls in type(node).__mro__:
        if cls in WEIGHTS:
            return WEIGHTS[cls]
    return 0


def calc(sql: str, dialect: str = DEFAULT_DIALECT) -> int:
    """
    Calculate the cyclomatic complexity of SQL.

    The complexity is 1 plus the weight of every construct in WEIGHTS found in the SQL. If the SQL
    contains more than one statement, the weights of all statements are summed.

    Args:
        sql (str): The SQL string, with one or more statements.
        dialect (str): The SQL dialect to use for parsing. See sqlglot documentation for supported dialects.

    Returns:
        int: The calculated cyclomatic complexity.

    Raises:
        TypeError: If sql is not a string.
        ValueError: If sql contains no statements, or the dialect is unknown.
        sqlglot.errors.ParseError: If sql can not be parsed with the dialect.
    """
    if not isinstance(sql, str):
        raise TypeError(f"sql must be a str, not {type(sql).__name__}")
    statements = [statement for statement in sg.parse(sql, dialect=dialect) if statement is not None]
    if not statements:
        raise ValueError("sql contains no statements")
    return 1 + sum(_weight(node) for statement in statements for node in statement.walk())


def crap(complexity: int, coverage: float) -> float:
    """
    Calculate CRAP1, the change risk anti-pattern score: complexity^2 * (1 - coverage/100)^3 + complexity.

    Args:
        complexity (int): The cyclomatic complexity, see calc.
        coverage (float): The coverage in percent, from 0 to 100.

    Returns:
        float: The CRAP score. It is never lower than the complexity.

    Raises:
        ValueError: If coverage is not from 0 to 100.
    """
    if not 0 <= coverage <= 100:
        raise ValueError(f"coverage must be from 0 to 100, not {coverage}")
    return complexity ** 2 * (1 - coverage / 100) ** 3 + complexity


def coverage_from_tests(tests: int, complexity: int) -> float:
    """
    Estimate the basis path coverage in percent from the number of tests.

    The complexity is the number of tests it takes to cover every basis path. When each test is
    assumed to cover one path, the coverage is the number of tests relative to the complexity.

    Args:
        tests (int): The number of tests.
        complexity (int): The cyclomatic complexity, see calc.

    Returns:
        float: The coverage in percent, at most 100.

    Raises:
        ValueError: If complexity is lower than 1.
    """
    if complexity < 1:
        raise ValueError(f"complexity must be at least 1, not {complexity}")
    return min(100.0, 100.0 * tests / complexity)


@dataclass(frozen=True)
class ModelScore:
    name: str
    complexity: int
    tests: int

    @property
    def coverage(self) -> float:
        return coverage_from_tests(self.tests, self.complexity)

    @property
    def crap(self) -> float:
        return crap(self.complexity, self.coverage)

    @property
    def missing(self) -> int:
        return max(0, self.complexity - self.tests)


class _Skipped(Exception):
    pass


def _sql_files(path: Path) -> list[Path]:
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    if path.is_file():
        return [path]
    # dbt compiles the generic tests of a schema.yml into a directory named schema.yml
    return sorted(
        file for file in path.rglob('*.sql')
        if not any(part.endswith(('.yml', '.yaml')) for part in file.relative_to(path).parts[:-1])
    )


def _find_manifest(paths: list[Path]) -> Path | None:
    for path in paths:
        resolved = path.resolve()
        for directory in [resolved, *resolved.parents]:
            if (directory / 'manifest.json').is_file():
                return directory / 'manifest.json'
    return None


def _read_json_object(path: Path) -> dict:
    try:
        content = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise ValueError(f"can not read {path}: {e}")
    if not isinstance(content, dict):
        raise ValueError(f"{path} is not a JSON object")
    return content


def _manifest_dialect(manifest: dict) -> str | None:
    adapter = manifest.get('metadata', {}).get('adapter_type')
    return ADAPTER_DIALECTS.get(adapter, adapter)


def _dialect_from_manifest(path: Path) -> str | None:
    try:
        return _manifest_dialect(_read_json_object(path))
    except ValueError as e:
        raise ValueError(f"can not read the dialect from {path}, use --dialect instead: {e}")


def _valid_dialect(dialect: str | None) -> str:
    dialect = dialect or DEFAULT_DIALECT
    sg.Dialect.get_or_raise(dialect)
    return dialect


def _resolve_dialect(dialect: str | None, manifest: Path | None, paths: list[Path]) -> str:
    manifest = manifest or _find_manifest(paths)
    if not dialect and manifest:
        dialect = _dialect_from_manifest(manifest)
    return _valid_dialect(dialect)


def _first_line(e: Exception) -> str:
    # a sqlglot error message can span several lines, with the offending SQL below the first
    return (str(e).splitlines() or [type(e).__name__])[0]


def _score_files(files: list[Path], dialect: str) -> tuple[dict[Path, int], dict[Path, str], list[Path]]:
    scores, failed, skipped = {}, {}, []
    for file in files:
        try:
            scores[file] = calc(file.read_text(encoding='utf-8'), dialect=dialect)
        except (OSError, UnicodeDecodeError, sg.errors.SqlglotError) as e:
            failed[file] = _first_line(e)
        except ValueError:
            skipped.append(file)
    return scores, failed, skipped


def _read_manifest(path: Path) -> dict:
    manifest = _read_json_object(path)
    if not isinstance(manifest.get('nodes'), dict):
        raise ValueError(f"{path} is not a dbt manifest")
    return manifest


def _test_counts(manifest: dict) -> Counter:
    """
    Count the tests of each node in the manifest.

    A generic test belongs to the node it is attached to only, even though a relationships test also
    depends on the node it refers to. A singular test belongs to every node it depends on, and so does
    a unit test. Disabled tests are not in the manifest nodes.
    """
    counts = Counter()
    for node in manifest['nodes'].values():
        if node.get('resource_type') == 'test':
            attached = node.get('attached_node')
            counts.update([attached] if attached else node.get('depends_on', {}).get('nodes', []))
    for unit_test in manifest.get('unit_tests', {}).values():
        counts.update(unit_test.get('depends_on', {}).get('nodes', []))
    return counts


def _models(manifest: dict) -> dict[str, dict]:
    project = manifest.get('metadata', {}).get('project_name')
    return {
        unique_id: node for unique_id, node in manifest['nodes'].items()
        if node.get('resource_type') == 'model' and (project is None or node.get('package_name') == project)
    }


def _compiled_sql(node: dict) -> str:
    if node.get('language', 'sql') != 'sql':
        raise _Skipped("it is not a SQL model")
    if not node.get('compiled_code'):
        raise _Skipped("it is not compiled, run dbt compile")
    return node['compiled_code']


def _score_models(manifest: dict, dialect: str) -> tuple[list[ModelScore], dict[str, str], dict[str, str]]:
    tests = _test_counts(manifest)
    scores, failed, skipped = [], {}, {}
    for unique_id, node in _models(manifest).items():
        try:
            scores.append(ModelScore(unique_id, calc(_compiled_sql(node), dialect=dialect), tests[unique_id]))
        except _Skipped as e:
            skipped[unique_id] = str(e)
        except sg.errors.SqlglotError as e:
            failed[unique_id] = _first_line(e)
        except ValueError:
            skipped[unique_id] = "it contains no SQL statements"
    return scores, failed, skipped


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='sqlcc',
        description='Calculate the cyclomatic complexity of SQL files, e.g. the models dbt compiles to target/compiled, '
                    'or with --crap the CRAP score of the models in a dbt manifest.',
        epilog='Exit status: 0 when everything is scored and nothing exceeds --max or --max-crap, '
               '1 when something exceeds them, '
               '2 when something can not be scored or the arguments are invalid.',
    )
    parser.add_argument('paths', nargs='*', type=Path, metavar='PATH',
                        help='SQL files, or directories which are searched for *.sql. '
                             'Compiled dbt tests, in directories named *.yml, are skipped. Not used with --crap.')
    parser.add_argument('-d', '--dialect',
                        help=f'The sqlglot dialect. Default is the adapter_type of the dbt manifest.json '
                             f'in or above PATH, or else {DEFAULT_DIALECT}.')
    parser.add_argument('--manifest', type=Path,
                        help=f'Use this dbt manifest.json to find the dialect. With --crap it is also where the models '
                             f'and tests are found, and the default is {DEFAULT_MANIFEST}.')
    parser.add_argument('--max', type=int, dest='max_score',
                        help='Exit with status 1 if the complexity of a file, or with --crap a model, is above this.')
    parser.add_argument('--crap', action='store_true',
                        help='Calculate the CRAP score of the models in the dbt manifest, from their compiled SQL and '
                             'tests. Run dbt compile first.')
    parser.add_argument('--max-crap', type=float,
                        help='With --crap, exit with status 1 if the CRAP score of a model is above this.')
    return parser


def _exceeding(values: dict, limit: float | None) -> dict:
    if limit is None:
        return {}
    return {key: value for key, value in values.items() if value > limit}


def _print_scores(scores: dict[Path, int]) -> None:
    for file, score in sorted(scores.items(), key=lambda item: (-item[1], str(item[0]))):
        print(f"{score:>6}  {file}")
    sys.stdout.flush()


def _print_diagnostics(scores: dict[Path, int], failed: dict[Path, str], skipped: list[Path],
                       exceeding: dict[Path, int], max_score: int | None) -> None:
    for file, message in failed.items():
        print(f"{file}: {message}", file=sys.stderr)
    for file in skipped:
        print(f"{file}: skipped, it contains no SQL statements", file=sys.stderr)
    for file, score in exceeding.items():
        print(f"{file}: complexity {score} exceeds --max {max_score}", file=sys.stderr)
    if scores:
        print(f"{len(scores)} files scored, max {max(scores.values())}, "
              f"average {sum(scores.values()) / len(scores):.1f}", file=sys.stderr)


def _print_models(scores: list[ModelScore]) -> None:
    print(f"{'CRAP':>9}  {'COMP':>5}  {'COV%':>6}  {'TESTS':>5}  {'MISSING':>7}  MODEL")
    for score in sorted(scores, key=lambda score: (-score.crap, score.name)):
        print(f"{score.crap:>9.1f}  {score.complexity:>5}  {score.coverage:>6.1f}  {score.tests:>5}  "
              f"{score.missing:>7}  {score.name}")
    sys.stdout.flush()


def _print_skipped(skipped: dict[str, str]) -> None:
    names_by_reason: dict[str, list[str]] = {}
    for name, reason in skipped.items():
        names_by_reason.setdefault(reason, []).append(name)
    for reason, names in names_by_reason.items():
        if len(names) > MAX_SKIPPED_LISTED:
            print(f"{len(names)} models skipped, {reason}", file=sys.stderr)
        else:
            for name in names:
                print(f"{name}: skipped, {reason}", file=sys.stderr)


def _print_model_diagnostics(scores: list[ModelScore], failed: dict[str, str], skipped: dict[str, str],
                             too_complex: dict[str, int], too_crappy: dict[str, float],
                             args: argparse.Namespace) -> None:
    for name, message in failed.items():
        print(f"{name}: {message}", file=sys.stderr)
    _print_skipped(skipped)
    for name, complexity in too_complex.items():
        print(f"{name}: complexity {complexity} exceeds --max {args.max_score}", file=sys.stderr)
    for name, score in too_crappy.items():
        print(f"{name}: CRAP {score:.1f} exceeds --max-crap {args.max_crap:g}", file=sys.stderr)
    if scores:
        print(f"{len(scores)} models scored, max CRAP {max(score.crap for score in scores):.1f}, "
              f"{sum(1 for score in scores if score.tests == 0)} without tests", file=sys.stderr)


def _run_files(args: argparse.Namespace) -> int:
    try:
        dialect = _resolve_dialect(args.dialect, args.manifest, args.paths)
        files = [file for path in args.paths for file in _sql_files(path)]
        if not files:
            raise ValueError("no .sql files found")
    except ValueError as e:
        print(f"sqlcc: error: {e}", file=sys.stderr)
        return 2

    scores, failed, skipped = _score_files(files, dialect)
    exceeding = _exceeding(scores, args.max_score)
    _print_scores(scores)
    _print_diagnostics(scores, failed, skipped, exceeding, args.max_score)
    if failed:
        return 2
    return 1 if exceeding else 0


def _run_crap(args: argparse.Namespace) -> int:
    try:
        manifest = _read_manifest(args.manifest or DEFAULT_MANIFEST)
        dialect = _valid_dialect(args.dialect or _manifest_dialect(manifest))
    except ValueError as e:
        print(f"sqlcc: error: {e}", file=sys.stderr)
        return 2

    scores, failed, skipped = _score_models(manifest, dialect)
    too_complex = _exceeding({score.name: score.complexity for score in scores}, args.max_score)
    too_crappy = _exceeding({score.name: score.crap for score in scores}, args.max_crap)
    _print_models(scores)
    _print_model_diagnostics(scores, failed, skipped, too_complex, too_crappy, args)
    if failed:
        return 2
    if not scores:
        print("sqlcc: error: no models could be scored", file=sys.stderr)
        return 2
    return 1 if too_complex or too_crappy else 0


def _main(argv: list[str] | None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.crap and args.paths:
        parser.error("--crap reads the models from the manifest, so PATH can not be used")
    if not args.crap and not args.paths:
        parser.error("the following arguments are required: PATH, or use --crap")
    if args.max_crap is not None and not args.crap:
        parser.error("--max-crap can only be used with --crap")
    return _run_crap(args) if args.crap else _run_files(args)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:
        # The reader of the output, e.g. head, is done. Point stdout to nowhere, so Python does not fail again
        # when it flushes it on exit. 141 is the status a shell reports for a process which got SIGPIPE.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


if __name__ == '__main__':
    sys.exit(main())
