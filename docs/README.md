# Documentation

How Draftwork Blog works and why it is built the way it is. The
[README](../README.md) covers installing and running it.

| Section | What is in it |
| ------- | ------------- |
| [API overview](api.md) | Every route, what it needs, and where the full reference is |
| [Concepts](concepts/index.md) | Topics that span several parts of the code: authentication, roles, security, the database, testing |
| [Features](features/index.md) | One page per folder under `app/`: what it owns, its rules, and the decisions behind it |

## Which page do I want?

- "How do I call this endpoint?" Open `/docs` on a running server, or see the
  [API overview](api.md).
- "Why does logging out end every token of the session?" A concept page.
- "Where is the code for roles, and what must I not break?" A feature page.

## Keeping these pages true

The docs describe the code as it is, so they change in the same commit as the
code they describe.

- A new folder under `app/` gets a page in `features/` and a row in its index.
- A new topic that spans features gets a page in `concepts/` and a row in its
  index.
- A new or changed route gets its row in [api.md](api.md). Request and
  response shapes are not written by hand anywhere: FastAPI generates them.
- Pages explain *why*. What the code does is in the code; a page earns its
  place by recording the reason, the rule, or the mistake that led to it.
