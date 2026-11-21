# Database

## What is this?

How the app talks to MongoDB: through PyMongo's own async client, with no
object-document mapper in between. Repositories work with plain documents and
turn them into Pydantic models by hand.

## Why no ODM?

An ODM would hide the
queries, the indexes and the transactions, which are the things worth seeing.
Mapping by hand costs a few lines per repository and keeps every query
visible in one place.

## Where the code is

| What | Where |
| ---- | ----- |
| Client, `DbDep`, indexes, transactions, `utcnow` | `app/core/database.py` |
| Opening and closing the client | The lifespan in `app/main.py` |
| `PyObjectId` | `app/core/types.py` |
| Cursor pagination | `app/core/pagination.py` |
| Queries | Each feature's `repository.py` |

## How it works

### One client, opened at startup

The app's lifespan creates a single `AsyncMongoClient`, which owns the
connection pool, and closes it at shutdown. Creating a client doesn't
connect, so startup pings the server: if MongoDB is unreachable the app
fails to start, after five seconds, instead of failing on the first request.

Routes get the database through `DbDep`; repositories receive it in their
constructor.

### A replica set is required

The app uses transactions, and MongoDB only has them on a replica set. A
single node is enough.

- `docker compose up -d --wait mongo` starts a single-node replica set.
- Hosted clusters such as MongoDB Atlas are replica sets already.
- A standalone `mongod` will run the app until the first transaction, which
  then fails.

### Transactions

`in_transaction(db, work)` runs `work(session)` so that all of its writes
happen or none do. Every query inside must be passed `session=`.

Use one when a check and a write must hold together. "Is there another
admin?" and "remove this one" is the example in the code. When two
transactions write the same document, MongoDB aborts one and the driver
retries it, so `work` may run more than once and must not have side effects
outside the database.

### Indexes

Each repository declares its collection's indexes with
`register_indexes(COLLECTION, [...])` when its module is imported, and
startup creates them. Creating an index that exists does nothing.

Unique indexes are what really prevent duplicates. Checking first and
inserting after would let two simultaneous requests both pass the check; the
repositories insert and translate the duplicate-key error instead.

| Collection | Indexes |
| ---------- | ------- |
| `users` | `email` unique, `username` unique |
| `refresh_tokens` | `expires_at` TTL (MongoDB deletes expired tokens itself), `user_id`, `family_id` |
| `roles` | `name` unique; `is_default` unique where it is true, so only one role can be the default |
| `permissions` | None; the `_id` is the permission's name |

### Ids

MongoDB calls the id `_id` and makes it an `ObjectId`. Models call it `id`,
and the repository maps one to the other. `PyObjectId` is the type for id
fields: it accepts an `ObjectId` or its 24-character hex string, writes a
string to JSON, and keeps the real `ObjectId` in `model_dump()`, which is
what a query needs. As a path parameter it turns a malformed id into a 422.

### Timestamps

Always timezone-aware UTC. The client is created with `tz_aware=True`.

`utcnow()` truncates to the millisecond, because that is all BSON stores.
Without it, a document just built and the same document read back would
differ by the microseconds, and an API would return two different
`created_at` values for one record.

### Pagination

List endpoints page by cursor, not by offset. The cursor names the last item
of the previous page, by its sort value and its id, and the next page is
"everything after that item". Deep pages stay fast, and items don't repeat
or go missing when new ones arrive.

- **The index must end with the sort field and then `_id`.** The list sorts
  by both, and an index that stops at the sort field makes MongoDB load every
  match and sort it in memory.
- **A cursor is not signed**, so a client can send anything. `decode_cursor`
  is where "anything" ends: what it lets through is a real date and a real
  id. A forged cursor can only move the client's own position; the list's
  filters are applied separately, so it cannot widen what the list contains.

## What a first-time reader should know

- A model is the read schema for every stored document, old ones included. A
  field added later needs a default or a backfill, or old documents become
  500s. For `Role` that would be every request of every holder.
- Changing an existing index's options makes startup fail. Drop the old index
  in the same change.
- Dropping a database on a replica set occasionally stalls for about five
  seconds. The tests therefore drop theirs once per run, not once per test.
