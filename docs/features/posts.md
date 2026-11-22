# Posts

`app/posts/`: writing, publishing and reading posts, and the tags on them.

## What it owns

- The `posts` collection.
- The routes under `/api/v1/posts`, plus `/api/v1/users/me/posts` and
  `/api/v1/tags`. The caller's own list sits under `/users/me` in the URL but
  is about posts, so it lives here.

It is the first feature to use the "own or any" permission check; see
[../concepts/roles-and-permissions.md](../concepts/roles-and-permissions.md).

## The modules

| Module | Holds |
| ------ | ----- |
| `rules.py` | Pure text rules: `slugify`, `normalize_tag`, `tag_problem`, `has_hidden_characters`, `make_excerpt` |
| `models.py` | `Post`, `PostView` (a post with its author), `TagCount` |
| `schemas.py` | What the API accepts and returns, the field limits, the feed's query parameters |
| `repository.py` | Queries, indexes, paging, the tag aggregation |
| `policy.py` | `may_see`: who may see a post. Plain Python, for features built on posts |
| `service.py` | `PostService`: who may change what, and the cleanup hooks run when a post is deleted |
| `dependencies.py` | Providers, the `PostId` path parameter, and where the cleanup hooks are registered |
| `router.py` | Three routers: posts, the caller's own posts, tags |

## The collection

`posts`:

| Field | Notes |
| ----- | ----- |
| `author_id` | The user who created it. Never changes |
| `title` | 1 to 200 characters, no control or invisible formatting characters |
| `slug` | Unique. The title's words, at most 80 characters, plus a dash and six random characters. Fixed at creation |
| `content` | Markdown, stored exactly as written, up to 100,000 characters |
| `excerpt` | Up to 300 characters. Generated from the content unless the author writes one |
| `tags` | Up to 10. Each 1 to 30 characters: letters and digits of any script, and `+ # . - _` |
| `status` | `draft` or `published` |
| `published_at` | Set the first time it is published, and kept after that |
| `comment_count` | Live comments, replies included; kept by the comments feature |
| `like_count` | Kept by the likes feature, in the transaction that stores each like |
| `created_at` | |
| `updated_at` | When the text or tags last changed. Publishing does not move it |

Indexes, by name: `slug` unique; `feed` on `(status, published_at, _id)`;
`feed_by_tag` on `(tags, status, published_at, _id)`; `feed_by_author` on
`(author_id, status, published_at, _id)`; `posts_by_author` on
`(author_id, created_at, _id)`.

## Who may do what

| Action | Needs |
| ------ | ----- |
| Read a published post, the feed, the tags | Nothing |
| Read a draft | Being its author, or any of `posts:read_drafts:any`, `posts:update:any`, `posts:publish:any`, `posts:delete:any` |
| Create | `posts:create` |
| Edit a draft | `posts:update:any`, or `posts:update:own` on your own post |
| Edit a published post | The same, and also the permission to publish it |
| Publish, unpublish | `posts:publish:any`, or `posts:publish:own` on your own post |
| Delete | `posts:delete:any`, or `posts:delete:own` on your own post |

Owning a post is not enough without the matching `:own` permission. An
author whose role loses `posts:update:own` keeps their posts but can no
longer edit them.

## Rules that are easy to break

- **A post you may not see is "not found", never "forbidden".** A 403 on
  someone's draft would confirm that the draft exists. `PostService._load`
  answers 404 first, using `may_see`; only then does `authorize` decide 403.
  Features built on posts ask `may_see` too: what hangs from a post is
  exactly as visible as the post.
- **Every public list filters on `status: "published"`.** The feed, the feed
  by tag, the feed by author, and the tag counts. A draft must not be
  reachable sideways.
- **Every slug gets a random ending, not only a taken one.** Drafts hold
  slugs too. When only a clash produced an ending, creating a post with a
  guessed title revealed whether someone had an unpublished post by that
  name.
- **An index for a paged list ends with the date and then `_id`.** The list
  sorts by both. Two indexes once stopped at the date, and MongoDB loaded
  every match to sort it: 13,300 index entries read for 21 posts. A test
  checks each index against its query's plan.
- **The author filter matches usernames, never emails.**
  `UserRepository.get_by_username` exists for public lookups so that a route
  cannot become a way to test which emails have accounts.
- **The author, slug, status and counters are the server's to set.** The
  request schemas refuse unknown fields, so a client sending `author_id` or
  `status` gets a 422 and not a silent success.
- **Changing what is public is publishing.** Editing a published post needs
  the publish permission as well, or a writer whose posts someone else
  approves could swap the text after approval.
- **`make_excerpt` only reads the first 2,000 characters.** Its patterns take
  time that grows with the square of the input on hostile text. 100,000
  unclosed brackets took 26 seconds before the bound was added.
- **Tags and titles refuse invisible characters.** A zero-width character
  would let a tag pose as `python` while being a different string, and a
  direction override can make a title display as something else. Titles may
  still use the two joiners that Persian and several Indian scripts need.

## Key decisions

- **The slug is fixed at creation.** Links keep working when a title changes.
  The cost is a slug that can drift from the title.
- **Slugs keep letters of every script, with their marks.** A title in Arabic
  or Hindi gives a readable slug. Punctuation and invisible characters are
  dropped; a title with nothing readable gives `post`.
- **Any `:any` permission on posts lets its holder see drafts.** Someone
  trusted to publish any post has to be able to see the one they publish.
  Each permission still covers only its own action.
- **No flag records whether an excerpt was generated.** It was generated if
  it equals what generating gives for the current content. A generated
  excerpt follows the content; one the author wrote is left alone; sending
  an empty excerpt goes back to a generated one.
- **`published_at` survives unpublishing**, and the feed is ordered by it. A
  post that is pulled and put back returns to its old place, among older
  posts, and does not appear as new.
- **A bad token is a 401 on `GET /posts/{slug}`, not "anonymous".** That
  route reads the token, and a client whose token expired should be told to
  refresh. The feed and the tags never read a token at all.
- **A response that depends on who asks is never cached.** A post read with a
  token, or any draft, is sent with `Cache-Control: private, no-store`.
- **Reading is by slug and writing by id.** The paths `/posts/{slug}` and
  `/posts/{post_id}` differ only in the parameter's name, which strict
  OpenAPI tooling may question. Accepted, for readable URLs.
- **Deleting a post runs cleanup hooks in the same transaction.** The post
  goes first, then whatever each feature registered: its comments, and the
  likes on it and on them. `PostService` imports nothing from those features.
- **`liked_by_me` comes through a lookup that likes registers**, the same
  way: one query per page, and false for everything when nobody is logged
  in. The feed therefore takes an optional login, and is never cached when
  read with one.
- **Lists carry summaries.** The feed returns the excerpt, not the content.
- **Authors are fetched once per page**, not once per post. A post whose
  author's account is gone is shown with `author: null`.
- **The list of tags is remembered for a minute** (`FASTAPI_TAGS_CACHE_SECONDS`,
  0 to switch it off). Counting tags reads every published post, and the
  answer is the same for everyone. Each worker keeps its own copy, so a new
  tag can take up to a minute to appear; the response's `Cache-Control`
  lets browsers and proxies keep it for another, so two minutes at most. `Remembered` in
  `app/core/ttl.py` does it, and lets only one request recount when the
  minute is up.
- **Markdown is not rendered or cleaned here.** Clients render it, and must
  treat title, excerpt and content as untrusted text.

## Worth knowing

- A deactivated author's posts stay, under their name; only the author's
  profile page goes. Hiding them is a moderator's decision, post by post.

## Tests

`tests/posts/test_posts.py` over HTTP, including the tests written from an
attacker's side and one test per permission held alone.
`tests/posts/test_post_rules.py` for slugs, tags, excerpts and field limits.
`tests/posts/test_tags_cache.py` for the remembered list of tags, on an app
built with remembering switched on.
Fixtures for authors, an editor and ready-made posts are in
`tests/conftest.py`.
