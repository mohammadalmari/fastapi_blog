# Comments

`app/comments/`: commenting on posts, replying to comments, editing and
deleting.

## What it owns

- The `comments` collection.
- The routes under `/api/v1/posts/{post_id}/comments` and `/api/v1/comments`.

It builds on posts: a comment is exactly as visible as the post it is on, and
the post carries a count of its comments.

## The modules

| Module | Holds |
| ------ | ----- |
| `models.py` | `Comment`, and `CommentView` (a comment with its author) |
| `schemas.py` | What the API accepts and returns, and what a comment may contain |
| `repository.py` | Queries, the one index, paging, the conditional counter updates, `remove` |
| `service.py` | `CommentService`: who may do what, and the transactions |
| `dependencies.py` | Providers for the repository and the service |
| `router.py` | Two routers: a post's comments, and a comment by its id |

## The collection

`comments`:

| Field | Notes |
| ----- | ----- |
| `post_id` | The post it is on |
| `author_id` | Who wrote it |
| `parent_id` | Null for a comment on the post; otherwise the comment it replies to |
| `content` | 1 to 5,000 characters with something visible in them; line breaks and tabs allowed, other control and invisible formatting characters refused |
| `is_deleted` | True only for a placeholder: a deleted comment kept because it has replies |
| `reply_count` | The number of replies |
| `like_count` | Zero for now; nothing maintains it yet |
| `created_at`, `updated_at` | `updated_at` moves when the comment is edited, and only then |

One index, `thread`, on `(post_id, parent_id, created_at, _id)`. It serves
both lists in order: a post's own comments (`parent_id` null) and the replies
to one comment.

The post's `comment_count` is the number of comments on it, replies included
and placeholders not.

## Who may do what

| Action | Needs |
| ------ | ----- |
| Read comments and replies | Nothing, if the post is visible to you |
| Comment, reply | `comments:create`, on a published post |
| Edit | `comments:update:own`, on your own comment, while its post is visible to you |
| Delete someone else's | `comments:delete:any`, while its post is visible to you |
| Delete your own | `comments:delete:own`, whether or not you can still see the post |

There is no "edit any comment" permission. A moderator may remove what
someone said, but may not change it. The author of a post has no special
rights over the comments on it.

## What deleting does

| The comment | What happens |
| ----------- | ------------ |
| A reply | Its row is removed |
| A comment with no replies | Its row is removed |
| A comment with replies | It stays as a placeholder: content erased, `is_deleted` true, shown with neither words nor author |
| A placeholder whose last reply is deleted | Its row is removed too |

So every row that exists is one a list shows. The first version kept every
deleted row and filtered them out when listing. A list then read all of them
to find the few still showing: 5,021 rows read to return 21, on a post where
someone had posted and deleted in a loop, and nothing could clean them up.

## Rules that are easy to break

- **A counter and the thing it counts are written in one transaction.**
  Creating a comment inserts it and raises the post's count; a reply also
  raises its parent's count; deleting lowers them. If any step fails, none
  happens, so a count can't drift from what it counts.
- **A condition is checked in the write that depends on it.** "The post is
  published" is part of the update that raises the post's count; "the comment
  is still there to be replied to" is part of the update that raises its
  reply count. A check made before the transaction would say the same thing a
  moment too early. One early check remains, "replies can't be replied to",
  because a comment's parent never changes.
- **Deleting is decided by conditional writes.** Each step of
  `CommentRepository.remove` carries its condition in its filter, so of two
  requests deleting the same comment exactly one succeeds, and the counts go
  down once.
- **A comment is as visible as its post.** When a post is hidden from the
  caller, its comments and their replies are "not found", by post id and by
  comment id alike. `app/posts/policy.py` has the rule, `may_see`.
- **A deleted comment is "not found", exactly like one that never existed.**
  On every route, for everyone. Any other answer would say that something
  used to be there. A placeholder is the one exception, and it is publicly
  listed anyway: it can be asked for its replies, and a reply to it is a 409.
- **The author, the post and the parent never come from the request body.**
  They come from who is asking and from the URL.

## Key decisions

- **Threads are one level deep.** A reply can't be replied to (409).
- **Comments are oldest first**, the order a conversation happened in, where
  the feed of posts is newest first.
- **Only published posts take comments.** A draft is a 404 to those who can't
  see it and a 409 to its author. A post that is deleted or pulled while a
  comment is on its way gives the same answers as if it had happened a
  moment earlier.
- **You can always take back your own words.** Deleting your own comment
  works even under a post that was pulled; otherwise pulling a post would
  freeze what others said there, and put it back unchanged if the post
  returned. Editing does not: nobody can see the result.
- **Deleting a post deletes its comments**, in the same transaction. Posts
  doesn't know about comments: `PostService` runs a list of cleanup hooks,
  and `get_post_service` registers `CommentRepository.delete_for_post`.
- **Lists read with a token are never cached**, since the comments of a
  draft are shown only to some.

## Known costs and limits

- **Every comment on a post writes that post's document**, so comments
  arriving together on one post take turns: MongoDB aborts all but one and
  the driver retries them. Measured in one process: 30 at once take 0.2 s,
  100 take 0.8 s, 300 take 2.9 s, all succeeding with exact counts.
  Concurrency buys no speed on a single post. The alternative, raising the
  count outside the transaction, would not abort but could leave the count
  wrong after a crash.
- **Deleting a post removes all its comments in one transaction.** A post
  with a very large discussion could exceed MongoDB's transaction time limit,
  and the delete would then fail.
- **No rate limit yet**, so nothing stops someone filling a post with
  comments.

## Not here yet

`liked_by_me` and the like counts arrive with likes, which will also have to
remove a comment's likes when the comment or its post is deleted.

## Tests

`tests/comments/test_comments.py` over HTTP. Besides the checklist for each
route it has: counters under concurrent writes, races between two deletes of
one comment, three tests that make a later step of a transaction fail and
check the earlier steps were undone, a test that each list reads only the
rows it returns, and the attacks on other people's comments and on hidden
posts. `tests/comments/test_comment_rules.py` covers what a comment may
contain and the owner-only check. The `post` and `make_comment` fixtures are
in `tests/conftest.py`; `make_comment` goes through the service, so the
counters are real.
