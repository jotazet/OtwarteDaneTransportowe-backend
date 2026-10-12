"""Blog post permissions per role (docs/api/blog.md).

Reads are public; create needs Blogger, Editor or Admin; update and delete
need Editor or Admin, or Blogger on their own post.
"""

import pytest

from blog.models import Post
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_DATA_PROVIDER,
    ROLE_EDITOR,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

POSTS_URL = '/api/blog/posts/'
ANONYMOUS = None


@pytest.fixture
def other_bloggers_post(make_user):
    author = make_user(ROLE_BLOGGER, 'post-author')
    return Post.objects.create(title='Original', author=author, content='body')


@pytest.mark.parametrize(
    'role, expected',
    [
        (ANONYMOUS, 401),
        (ROLE_DATA_PROVIDER, 403),
        (ROLE_HELPER, 403),
        (ROLE_BLOGGER, 201),
        (ROLE_EDITOR, 201),
        (ROLE_ADMIN, 201),
    ],
)
def test_create_post_by_role(role, expected, make_user, client_as):
    user = make_user(role)

    response = client_as(user).post(
        POSTS_URL, {'title': 'New post', 'tags': ['gtfs'], 'content': 'Body'}, format='json'
    )

    assert response.status_code == expected, response.data
    if expected == 201:
        assert response.data['author'] == user.id
        assert response.data['author_username'] == user.username
        assert Post.objects.get(pk=response.data['id']).author == user
    else:
        assert not Post.objects.exists()


def test_create_ignores_author_in_payload(make_user, client_as):
    """The author is always the caller; a Blogger cannot post as someone else."""
    blogger = make_user(ROLE_BLOGGER)
    someone_else = make_user(ROLE_BLOGGER)

    response = client_as(blogger).post(
        POSTS_URL,
        {'title': 'Spoofed', 'content': 'Body', 'author': someone_else.id},
        format='json',
    )

    assert response.status_code == 201, response.data
    assert response.data['author'] == blogger.id


@pytest.mark.parametrize(
    'role, expected',
    [
        (ANONYMOUS, 401),
        (ROLE_DATA_PROVIDER, 403),
        (ROLE_HELPER, 403),
        (ROLE_BLOGGER, 403),
        (ROLE_EDITOR, 200),
        (ROLE_ADMIN, 200),
    ],
)
def test_update_someone_elses_post_by_role(
    role, expected, other_bloggers_post, make_user, client_as
):
    client = client_as(make_user(role))

    response = client.patch(
        f'{POSTS_URL}{other_bloggers_post.id}/', {'title': 'Edited'}, format='json'
    )

    assert response.status_code == expected, response.data
    other_bloggers_post.refresh_from_db()
    assert other_bloggers_post.title == ('Edited' if expected == 200 else 'Original')
    # Editing never transfers authorship.
    assert other_bloggers_post.author.username == 'post-author'


@pytest.mark.parametrize(
    'role, expected',
    [
        (ANONYMOUS, 401),
        (ROLE_DATA_PROVIDER, 403),
        (ROLE_HELPER, 403),
        (ROLE_BLOGGER, 403),
        (ROLE_EDITOR, 204),
        (ROLE_ADMIN, 204),
    ],
)
def test_delete_someone_elses_post_by_role(
    role, expected, other_bloggers_post, make_user, client_as
):
    client = client_as(make_user(role))

    response = client.delete(f'{POSTS_URL}{other_bloggers_post.id}/')

    assert response.status_code == expected
    assert Post.objects.filter(pk=other_bloggers_post.pk).exists() == (expected != 204)


def test_blogger_can_update_and_delete_own_post(make_user, client_as):
    blogger = make_user(ROLE_BLOGGER)
    post = Post.objects.create(title='Mine', author=blogger, content='body')
    client = client_as(blogger)

    response = client.patch(f'{POSTS_URL}{post.id}/', {'title': 'Mine, edited'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['title'] == 'Mine, edited'

    response = client.put(
        f'{POSTS_URL}{post.id}/',
        {'title': 'Mine, replaced', 'tags': [], 'content': 'new body'},
        format='json',
    )
    assert response.status_code == 200, response.data
    assert response.data['content'] == 'new body'

    response = client.delete(f'{POSTS_URL}{post.id}/')
    assert response.status_code == 204
    assert not Post.objects.filter(pk=post.pk).exists()


@pytest.mark.parametrize('role', [ANONYMOUS, ROLE_DATA_PROVIDER, ROLE_HELPER, ROLE_BLOGGER])
def test_posts_are_publicly_readable(role, other_bloggers_post, make_user, client_as):
    client = client_as(make_user(role))

    response = client.get(POSTS_URL)
    assert response.status_code == 200
    assert [row['id'] for row in response.data['results']] == [other_bloggers_post.id]

    response = client.get(f'{POSTS_URL}{other_bloggers_post.id}/')
    assert response.status_code == 200
    assert response.data['content'] == 'body'
