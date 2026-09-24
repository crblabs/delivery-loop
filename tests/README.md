# tests

The tests of this package live here. One test module per source module, named
after it.

`fixtures/` holds the data a test reads rather than builds. `fixtures/supervisor/`
holds the supervisor decision table, which is the policy as data: every row is
one cell of the table, and the test binds each row to the decider.

A test imports a module the way a caller does, through the package: `from core
import supervisor_decide`. No test manipulates `sys.path`.
