# sqlcc
A [cyclomatix complexity](https://en.wikipedia.org/wiki/Cyclomatic_complexity) 
calculator for SQL

## Usage

```
    ...
    cc = sqlcc('select a, b from foo')
    ...
```
which returns a number representing the cyclomatic complexity of the SQL.