# Concepts

Topics that span more than one feature. Each page follows the same outline:
what it is, why it exists, where the code is, how it works, and what a
first-time reader should know.

| Page | What it covers |
| ---- | -------------- |
| [authentication.md](authentication.md) | The two tokens, the refresh cookie, rotation and reuse detection, logout, and the Origin check that stands in for CSRF protection |
| [roles-and-permissions.md](roles-and-permissions.md) | The permission catalog, roles, how a request is checked, "you can't grant what you don't hold", and the rules that keep the system administrable |
| [security.md](security.md) | The debug switch, what production mode turns on, response headers, CORS, the password policy, and limits on expensive work |
| [database.md](database.md) | MongoDB without an ODM, why a replica set is required, transactions, indexes, and timestamps |
| [testing.md](testing.md) | How the tests are set up, the rules for writing them, and how the suite stays fast |

## Adding a concept

Create `docs/concepts/<topic>.md` with the outline above and add a row here.
