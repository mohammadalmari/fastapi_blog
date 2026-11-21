# Testing

## What is this?

How the tests are set up, the rules for writing them, and how the suite is
kept fast. About 270 tests run in under five seconds.

## Why these rules exist

A review of the first auth work found tests that would have passed with the
feature broken, tests that depended on the developer's shell, and no test
for the race the feature was built to prevent. They shared one cause: the
tests were written after the code, to confirm it. The rules below are the
response.

## Where the code is

| What | Where |
| ---- | ----- |
| Fixtures | `tests/conftest.py` |
| Request helpers and shared constants | `tests/helpers.py` |
| HTTP tests, one module per feature | `tests/test_auth.py`, `tests/test_roles.py`, ... |
| Unit tests of pure rules | `tests/test_passwords.py`, `tests/test_tokens.py`, `tests/test_user_schemas.py`, `tests/test_role_schemas.py`, `tests/test_rbac_rules.py` |

## How the tests are set up

- **A real MongoDB.** Nothing is mocked by default. Each run uses a database
  with a random name, one per worker when parallel, dropped at the end.
- **One shared app.** Building an app and its indexes costs about 100 ms, so
  tests share one, and collections are emptied after every test. A module
  that needs another app, production mode for instance, overrides the
  module-scoped `settings_overrides` fixture and gets its own.
- **Cheap passwords.** Tests hash with minimum-cost Argon2. A test about the
  production hasher asks for the `real_password_hashing` fixture, and one
  test pins that the app itself uses the recommended parameters.
- **Ready-made people.** `user` (a reader) and `admin` are put straight into
  the database; `user_headers`, `admin_headers` and `headers_for(someone)`
  log them in. `make_user("name", "role")` and `make_role("name", Perm.X)`
  build others. Only tests about signing up or logging in use those routes.
- **A known starting state.** Every test begins with the permission catalog
  and the two system roles in place.
- **Isolated from the machine.** `FASTAPI_*` variables in the shell are
  dropped, settings the tests assert on are pinned, and logs go to a
  temporary folder.
- **Random order.** pytest-randomly shuffles the tests on every run.

Which server: `TEST_DATABASE_URL` if set, else `DATABASE_URL`, else the
Compose database. Both are read from `.env` or the shell.

## The rules

Writing them:

1. **List the cases before writing code.** The normal path, each way it is
   refused, and each state it must leave behind.
2. **Test first for bugs.** A fix starts with a test that fails.
3. **Prove a test can fail.** Break the behaviour once and watch it go red.
4. **One checklist for every endpoint:** success, invalid input, not logged
   in, not allowed, not found, conflict, what changed in the database.
5. **Security features get an attack test,** written from the attacker's
   side.
6. **Isolate tests from the machine.**
7. **Assert on meaning.** Parse cookies and tokens; don't match header text.
8. **Choose the level deliberately.** HTTP for behaviour, unit tests for pure
   logic, and a patch only to simulate a failure.

Keeping the suite small and fast:

9. **One HTTP test per behaviour.** Edge cases go into parametrized unit
   tests on the pure function.
10. **No duplicates.** Name the behaviour no existing test covers before
    adding one.
11. **A time budget:** the full suite under two minutes.

## Rule 3 in practice

After each milestone, every new behaviour is broken on purpose, one at a
time, and its test is run. A test that stays green is either testing nothing
or guarding dead code, and both have happened:

- A validator for un-encodable passwords turned out to be dead code:
  Pydantic already rejected the input. It was removed.
- Nothing proved the app used the production hasher, because every test used
  the cheap one. A test was added.
- A step in the startup sync was redundant; the step after it did the same
  work. It was removed.

## Commands

```bash
uv run pytest                                       # everything
uv run pytest -n auto                               # in parallel
uv run pytest --cov=app --cov-report=term-missing   # lines no test reaches
uv run pytest --durations=10                        # the slowest tests
uv run pytest -p randomly --randomly-seed=<n>       # repeat a random order
```

## What a first-time reader should know

- Async test modules set `pytestmark = pytest.mark.anyio`.
- Values inside `pytest.mark.parametrize` must be the same in every process,
  so no random ids: they become test names, and parallel workers must agree.
- A test must not leave state on the shared app. Dependency overrides are
  cleared after each test; anything else is the test's to undo.
- The `permissions` collection is not reset between tests, since the catalog
  is the same for all of them. A test that adds a row there removes it.
- For a suite this small, parallel is slower than serial, because each
  worker starts its own app. It is there for when the suite grows.
