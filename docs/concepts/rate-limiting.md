# Rate limiting

## What is this?

A cap on how often one caller may do one thing. Past the cap the answer is
`429 Too Many Requests`, with a `Retry-After` header saying how many seconds
to wait.

## Why it exists

Three kinds of request are worth limiting, and each for its own reason:

- **Guessing.** Logging in, and changing a password (which asks for the
  current one), let someone try passwords. Slow hashing makes each try
  expensive; a limit makes them few.
- **Expensive work.** Every like and every comment is a transaction on a
  shared document, and an avatar is a picture to decode. In the likes review,
  300 likes at once on one post caused about 3,000 retried transactions.
- **Making accounts.** Every other limit counts by account, so accounts
  themselves must not be free to mass-produce.

Reading is not limited. It is cheap, cacheable, and a proxy in front of the
app is the better place to slow a crawler.

## Where the code is

| What | Where |
| ---- | ----- |
| The limiter, and `limit_by_address` | `app/core/ratelimit.py` |
| `limit_by_user` | `app/auth/dependencies.py` |
| The limits themselves | `app/core/settings/security.py` |
| The 429 | `TooManyRequestsError` in `app/core/errors.py` |
| The account lock at login | `AuthService.login` |

## How it works

### The limits

| Route | Counted by | Limit |
| ----- | ---------- | ----- |
| `POST /auth/login` | Address | 30 in 5 minutes |
| `POST /auth/login`, failures only | Account | 10 in 15 minutes |
| `POST /auth/register` | Address | 10 an hour |
| `POST /users/me/password` | User | 5 in 15 minutes |
| Commenting and replying, together | User | 30 a minute |
| Liking and unliking, together | User | 120 a minute |
| `PUT /users/me/avatar` | User | 10 an hour |

They are constants, set well above what a person does and well below what a
script can. Nobody honest should ever meet one.

They apply in debug mode too. The rule for hardening is that what needs
HTTPS, or gets in the way of local work, is switched on only in production;
these do neither, and one behaviour everywhere means the ordinary test app
exercises them.

### Counting

The counters are documents in the `rate_limits` collection, not numbers in
the process. A count in memory would be per worker, so four workers would
allow four times the limit, and it would be lost on every restart.

Time is cut into windows of the rule's length. Each (rule, caller, window)
has one counter, and a request does one thing to it: "add one and tell me
the result", as a single database operation. Twelve requests at the same
moment therefore get twelve different results, and exactly the allowed
number pass. Reading the count and then writing it back would let several
see "one left".

A counter is removed by MongoDB when its window ends (a TTL index). The
caller is stored as a keyed hash (HMAC with the app's secret key), so the
collection holds no addresses or emails. A plain hash would not do: there
are few enough IPv4 addresses to hash them all and look the answer up.

### By user where there is a login, by address where there is not

An address may be shared by a whole office or a phone network, and one
person can have many. An account is one person. So routes that need a login
are counted by user (`limit_by_user`), and only login and sign-up, which
have no user yet, are counted by address (`limit_by_address`).

Behind a proxy the address the app sees is the proxy's, and every visitor
would share one allowance. The server must be told to trust the proxy's
`X-Forwarded-For` header (`--forwarded-allow-ips`, the same setting the
HTTPS redirect needs), and to trust only the proxy: otherwise the header is
the client's to write, and each request could claim a new address.

An IPv4 address is counted as itself. An IPv6 connection comes with a whole
network, 2^64 addresses, so counting each one would let a single customer
use a new address per request; IPv6 callers are counted by their /64
network. When the server has no address for a caller at all (it listens on
a unix socket), everyone shares one allowance: put a proxy in front and
trust its header.

### The account lock

Counting login attempts by address stops one machine trying many accounts.
It does not stop many machines trying one account. So failed logins are
also counted against the account, wherever they come from, and after ten in
fifteen minutes that account can't be logged into, with the right password
either, until the window ends.

- **A place is taken before the password is checked, and given back if the
  login succeeds.** Only failures are meant to count, and the obvious way is
  to count one once the check has failed. But the check is slow on purpose,
  and forty guesses sent together would each be let in before any had been
  counted: the review measured 36 of 40 verified against a limit of 10.
  Taking the place first makes the count and the decision one operation.
- It is keyed on what was typed, normalized as the lookup normalizes it,
  whether or not an account by that name exists. A name with no account
  locks exactly like one with, so the lock reveals nothing.
- The lock is decided before the password is looked at, which also saves
  the cost of hashing while it holds.
- The email and the username are counted apart, so an account can be tried
  by both: twice the guesses, no more.

The price is that someone who knows your username can keep you from logging
in with it, fifteen minutes at a time, by failing on purpose. Logging in
with your email still works unless they know that too, and sessions already
open are not affected. That is the usual trade: the alternative lets a
botnet guess without limit.

### Using it on a route

```python
@router.post("", dependencies=[Depends(limit_by_user(COMMENTS_PER_USER))])
```

Add the rule to `app/core/settings/security.py`, the dependency to the
route, and 429 to its `responses=`.

## What a first-time reader should know

- A fixed window allows a burst at its edge: a whole allowance at the end of
  one window and another at the start of the next. The limits are chosen
  with that in mind. A sliding window is smoother and costs more per request.
- A request without a valid login is refused before it is counted, so
  strangers can't use up a user's allowance.
- The limiter runs after FastAPI has read the request body. A caller over
  their limit can still make the server receive an upload of up to 2 MB
  before being refused; the expensive part, decoding it, is skipped.
  Refusing before the body arrives is a job for the proxy.
- The limiter costs one small write per limited request. That is cheap next
  to what those requests do, and it is why reads are left alone.
- Browsers hide most response headers from pages on another origin.
  `Retry-After` and `X-Request-ID` are listed in the CORS configuration so a
  frontend can read them.
- Tests empty the counters between tests like every other collection, and
  spend an allowance directly on the limiter rather than through dozens of
  requests; see `tests/auth/test_rate_limits.py`.
