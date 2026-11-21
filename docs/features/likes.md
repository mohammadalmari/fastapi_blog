# Likes

`app/likes/`: liking posts and comments, and taking a like back.

## What it owns

- The `likes` collection.
- Four routes: `PUT` and `DELETE` on `/api/v1/posts/{post_id}/like` and on
  `/api/v1/comments/{comment_id}/like`.

It builds on posts and comments. They carry the like counts and show
`liked_by_me`; this feature is the only one that changes either.

## The modules

| Module | Holds |
| ------ | ----- |
| `repository.py` | Queries, the two indexes, and the filters the tests check against their plans |
| `service.py` | `LikeService`: who may like what, and the transactions |
| `dependencies.py` | Providers for the repository and the service |
| `router.py` | The four routes |

There is no `schemas.py` and no `models.py`: the routes take no body and
return none, and a like is never read back whole.

## The collection

`likes`:

| Field | Notes |
| ----- | ----- |
| `user_id` | Who likes it |
| `target_type` | `post` or `comment` |
| `target_id` | The post or comment |
| `post_id` | The post it sits under: the post itself, or the comment's post |
| `created_at` | |

Two indexes:

- `one_like_each`, unique on `(user_id, target_type, target_id)`. One like
  per person per thing. It also answers "which of these has this person
  liked?" for a whole page at once.
- `likes_under_post` on `(post_id, target_id)`, for cleaning up: everything
  under a post, or everything on one comment.

`post_id` is stored on every like for that second index. Without it,
deleting a post would have to collect the ids of all its comments first.

## Who may do what

| Action | Needs |
| ------ | ----- |
| Like a post or comment | `likes:create`, and being able to see it |
| Take your like back | A login, and nothing else |

Whoever can see something can like it, a draft included: a like shows
nothing to anyone who couldn't already see the post. Taking a like back needs
no permission, so that losing the right to like doesn't trap the likes
already given.

## Rules that are easy to break

- **Liking is `PUT` and unliking is `DELETE`; there is no toggle.** Each
  request says what the result should be, so a double click or a retry after
  a timeout ends where a single request would. A toggle sent twice undoes
  itself.
- **The unique index is what makes a repeated like harmless**, not a check
  beforehand. Six identical requests at once all get 204 and one like is
  stored: the database refuses the other five inserts, however they were
  timed. A "do I already like it?" query before inserting would let two of
  them through.
- **A like and the count that includes it are written in one transaction.**
  The like is inserted first, so a repeated like stops at the index without
  touching the post or comment. Then the count is raised. If that can't be
  done, the transaction is undone and the answer is 404.
- **"You may see it" is checked when the like is written**, not when the
  request arrives. For a post it is part of the counting update's filter:
  the post must be published, unless the caller would see it as a draft too.
  For a comment the filter says "still there and not a placeholder", and the
  comment's post is read inside the transaction. A like that arrives while a
  post is being pulled is therefore refused, as it would have been a moment
  later.
- **Only a like that was really removed lowers the count.** That is what
  keeps a repeated unlike from lowering it twice.
- **What is hidden answers exactly like what doesn't exist.** A like on
  someone else's draft, on a comment under it, or on a deleted comment is
  the same 404 as an id that was never used.
- **Taking a like back has one answer: 204.** Whether you had a like or not,
  whether the thing is visible, hidden, deleted or never existed. Nothing is
  looked up about the thing at all. The first version answered 404 when
  there was no like and nothing visible, and that let someone who had liked
  a post learn later what became of it: 204 meant "pulled, your like is
  still there", 404 meant "deleted, it went with the post". The cost is that
  a mistyped id is not reported.
- **Likes go with what they are on.** Deleting a comment deletes its likes,
  also when the comment stays as a placeholder (its count goes back to zero).
  Deleting a post deletes the likes on it and on every comment under it. All
  in the same transaction as the delete.
- **A response that says what the reader liked is kept from shared caches.**
  The public lists send `Vary: Authorization`, and `private, no-store` when
  read with a token; `app/core/caching.py` has the two helpers. Without
  `Vary`, a cache that ignores the token could hand a logged-in reader the
  copy it kept for visitors.

## Key decisions

- **Posts and comments don't import likes.** `PostService` and
  `CommentService` each take two things from outside: a list of cleanup
  hooks to run when something is deleted, and a lookup for "which of these
  has this user liked?". `get_post_service` and `get_comment_service`
  register `LikeRepository`'s methods for both. With nothing registered,
  nothing is liked and nothing extra is cleaned up.
- **`liked_by_me` is always there, and false for a visitor**, so a client
  reads one shape whether or not anyone is logged in. A page of posts or
  comments costs one extra query for it, not one per item.
- **The routes return 204 with no body.** The client already knows the
  result: it is what it asked for.

## Known costs and limits

- **Every like on a post writes that post's document**, and every like on a
  comment writes that comment's. Likes arriving together on one thing take
  turns: MongoDB aborts all but one and the driver retries them. Measured
  during the review, on one post, from different users: 20 at once took
  0.1 s, 80 took 0.8 s (about 475 aborted attempts), 300 took 3.1 s (about
  3,000). Every request succeeded and the count was exact. The same burst
  slows whatever else writes that post, such as a new comment.
- **No rate limit yet**, so nothing stops someone liking and unliking in a
  loop to cause that load. Rate limiting comes with the hardening milestone.
- **Deleting a post removes every like under it in one transaction**, like
  its comments. A post with a very large number of likes could exceed
  MongoDB's transaction time limit, and the delete would then fail.
- **A deactivated user's likes stay counted**, and they can't log in to take
  them back. What deactivation does to them is decided with account
  management.
- **Nothing lists who liked something**, and nothing lists what a user has
  liked. Only counts and `liked_by_me` are shown.

## Tests

`tests/likes/test_likes.py` over HTTP. Most tests run twice, once on a post
and once on a comment, through the `target` fixture: the two are meant to
behave the same. Besides the checklist it has the attacks (the same like
fired in parallel, likes on hidden things, unliking to learn what became of
a post), tests that change or remove the target between the check and the
write, tests that make a later step of a transaction fail and check the
earlier step was undone, and one that checks every query on the
collection is read off an index.
