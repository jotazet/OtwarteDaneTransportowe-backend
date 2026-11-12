# Blog API

## `GET /api/blog/posts/`

List of blog posts (paginated DRF envelope, like all lists).

**Permissions:** public.

**Query params:**

- `page` - page number (default 1).
- `page_size` - default 4, maximum 50.

**Response 200 (`results` shortened):**

```json
{
  "count": 9,
  "next": "https://api.example.org/api/blog/posts/?page=2",
  "previous": null,
  "results": [
    {
      "id": 1,
      "title": "Nowe dane GTFS",
      "author": 3,
      "author_username": "admin",
      "tags": ["gtfs", "transport"],
      "content": "Skrocona tresc do 400 znakow...",
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
}
```

In the list, `content` is a preview: a text longer than 400 characters is cut to its first 400 characters followed by `...`.

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
- `tags` - list of max. 5 tags (`blog.too_many_tags`), each max. 16 characters (a longer tag gets DRF's built-in `max_length` error, keyed by its index).
- `content` - required.
- `image` - optional image file, at most `MAX_IMAGE_FILE_SIZE_BYTES` (`blog.image_too_large`). In responses, `image` is the absolute URL of the file (see below) or `null`.

## `GET /internal-media/blog/{path}`

Serves blog post images, without a token. Use the URL from the post's `image` field rather than building it. Only the `blog/` part of the media directory is served; feed files are available only through the feed download endpoints.

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
  "reaction": ""
}
```

**Rules:**

- Reactions are limited per IP.
- At most 10 active new reactions per IP within 24h; the 11th returns `429` with `{"code": "blog.reaction_daily_limit", "details": {}}`. If the server cannot determine the client IP, `400` with `{"code": "blog.client_ip_unknown", "details": {}}`.
- Responses: `201` first reaction, `200` change of an existing one, `204` empty reaction. Two simultaneous first reactions from one IP (double click) give `201` and `200`, never `500`.
- The endpoint supports only `POST` and `OPTIONS`; list/retrieve are deliberately disabled so as not to expose IP addresses.
