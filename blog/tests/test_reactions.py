"""Blog reactions: trusted-proxy IP resolution, daily limits and soft-remove."""
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from rest_framework.test import APIClient

from OtwarteDaneTransportowe.request_ip import get_client_ip
from blog.models import Post, Reaction

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def post():
    author = get_user_model().objects.create_user('author', 'author@example.com', 'password')
    return Post.objects.create(title='Post', author=author, content='body')


# ---------------------------------------------------------------------------
# get_client_ip helper
# ---------------------------------------------------------------------------

def _request(remote, xff=None):
    headers = {'REMOTE_ADDR': remote}
    if xff is not None:
        headers['HTTP_X_FORWARDED_FOR'] = xff
    return RequestFactory().get('/', **headers)


def test_xff_ignored_from_untrusted_remote(settings):
    settings.TRUSTED_PROXY_CIDRS = []
    request = _request('203.0.113.7', xff='198.51.100.1')
    assert get_client_ip(request) == '203.0.113.7'


def test_xff_honoured_from_trusted_proxy(settings):
    settings.TRUSTED_PROXY_CIDRS = ['10.0.0.0/8']
    request = _request('10.1.2.3', xff='198.51.100.1, 10.1.2.3')
    assert get_client_ip(request) == '198.51.100.1'


def test_xff_ignored_when_remote_outside_trusted_cidr(settings):
    settings.TRUSTED_PROXY_CIDRS = ['10.0.0.0/8']
    request = _request('203.0.113.7', xff='198.51.100.1')
    assert get_client_ip(request) == '203.0.113.7'


def test_malformed_cidr_entries_are_skipped(settings):
    settings.TRUSTED_PROXY_CIDRS = ['not-a-cidr', '10.0.0.0/8']
    request = _request('10.1.2.3', xff='198.51.100.1')
    assert get_client_ip(request) == '198.51.100.1'


# ---------------------------------------------------------------------------
# Reaction create/update/soft-remove
# ---------------------------------------------------------------------------

def test_create_update_and_soft_remove_reaction(api_client, post):
    url = f'/api/blog/reactions/{post.id}/'

    response = api_client.post(url, {'reaction': 'like'})
    assert response.status_code == 201
    assert Reaction.objects.get(post=post).reaction == 'like'

    response = api_client.post(url, {'reaction': 'wow'})
    assert response.status_code == 200
    assert Reaction.objects.get(post=post).reaction == 'wow'

    # Empty reaction soft-removes: row kept, value nulled.
    response = api_client.post(url, {'reaction': ''})
    assert response.status_code == 204
    row = Reaction.objects.get(post=post)
    assert row.reaction is None


def test_daily_limit_of_ten_active_reactions(api_client, settings):
    author = get_user_model().objects.create_user('author2', 'a2@example.com', 'password')
    posts = [
        Post.objects.create(title=f'P{i}', author=author, content='x')
        for i in range(11)
    ]
    for post in posts[:10]:
        assert api_client.post(f'/api/blog/reactions/{post.id}/', {'reaction': 'like'}).status_code == 201

    response = api_client.post(f'/api/blog/reactions/{posts[10].id}/', {'reaction': 'like'})
    assert response.status_code == 429


def test_your_reaction_ignores_forged_xff(api_client, post, settings):
    settings.TRUSTED_PROXY_CIDRS = []
    api_client.post(f'/api/blog/reactions/{post.id}/', {'reaction': 'like'})

    # Same client forging XFF must still see its own reaction (identity from
    # REMOTE_ADDR)...
    response = api_client.get(f'/api/blog/posts/{post.id}/', HTTP_X_FORWARDED_FOR='198.51.100.1')
    assert response.status_code == 200
    assert response.data['your_reaction'] == 'like'

    # ...and a reaction stored under a spoofable header identity cannot be
    # read back by naming that IP.
    Reaction.objects.create(post=post, ip_address='198.51.100.99', reaction='angry')
    response = api_client.get(f'/api/blog/posts/{post.id}/', HTTP_X_FORWARDED_FOR='198.51.100.99')
    assert response.data['your_reaction'] == 'like'
