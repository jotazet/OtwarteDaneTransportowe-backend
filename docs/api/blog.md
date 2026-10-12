# Blog API

## `GET /api/blog/posts/`

List of blog posts (paginated DRF envelope, like all lists).

**Permissions:** public.

**Query params:**

- `page` - page number (default 1).
- `page_size` - default 4, maximum 50.

**Response 200 (`results` shortened):**

```json
[
  {
    "id": 1,
    "title": "Nowe dane GTFS",
    "author": 3,
    "author_username": "admin",
    "tags": ["gtfs", "transport"],
    "content": "Skrocona tresc do 150 znakow...",
    "image": null,
    "date": "2026-04-27T10:00:00Z",
    "updated_at": "2026-04-27T10:00:00Z",
    "reactions_summary": {
      "like": 3,
      "dislike": 0,
      "love": 1,
      "haha": 0,
      "wow": 0,
      "sad": 0,
      "angry": 0
    },
    "your_reaction": "like"
  }
]
```

## `GET /api/blog/posts/{id}/`

Post details. Unlike in the list, the `content` field contains the full text.

**Permissions:** public.

## `POST /api/blog/posts/`

Creates a blog post. The author is set automatically to the logged-in user.

**Permissions:** `Blogger`, `Editor` or `Admin`.

**Request: JSON, or multipart when an image is included:**

```json
{
  "title": "Nowy wpis",
  "tags": ["gtfs", "api"],
  "content": "Pelna tresc wpisu"
}
```

**Fields:**

- `title` - required, max. 24 characters.
- `tags` - list of max. 5 tags, each max. 16 characters.
- `content` - required.
- `image` - optional image file.

## `PUT/PATCH/DELETE /api/blog/posts/{id}/`

Edits or deletes a post.

**Permissions:** `Blogger` can edit/delete **only their own** posts; `Editor` and `Admin` can edit and delete any posts.

## `POST /api/blog/reactions/{post_id}/`

Creates, changes or removes the reaction for the `(post, IP)` pair.

**Permissions:** public.

**Request:**

```json
{
  "reaction": "like"
}
```

Allowed reactions:

```text
like, dislike, love, haha, wow, sad, angry
```

Removing a reaction:

```json
{
  "reaction": null
}
```

or:

```json
{
  "reaction": ""
}
```

**Rules:**

- Reactions are limited per IP.
- At most 10 active new reactions per IP within 24h.
- The endpoint supports only `POST` and `OPTIONS`; list/retrieve are deliberately disabled so as not to expose IP addresses.
