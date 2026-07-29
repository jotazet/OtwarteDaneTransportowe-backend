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


# ---------------------------------------------------------------------------
# Per-visitor identity behind a reverse proxy (shared-reaction regression)
# ---------------------------------------------------------------------------

def test_visitors_behind_trusted_proxy_get_separate_reactions(api_client, post, settings):
    """Regression: with the proxy network trusted, two visitors arriving through
    the same nginx must NOT share one reaction row (previously the second
    visitor overwrote the first — one global reaction per post)."""
    settings.TRUSTED_PROXY_CIDRS = ['172.16.0.0/12']
    url = f'/api/blog/reactions/{post.id}/'

    first = api_client.post(url, {'reaction': 'like'},
                            REMOTE_ADDR='172.18.0.1', HTTP_X_FORWARDED_FOR='203.0.113.10')
    second = api_client.post(url, {'reaction': 'angry'},
                             REMOTE_ADDR='172.18.0.1', HTTP_X_FORWARDED_FOR='198.51.100.77')

    assert first.status_code == 201
    assert second.status_code == 201, 'second visitor must create its own row, not update'
    stored = dict(Reaction.objects.values_list('ip_address', 'reaction'))
    assert stored == {'203.0.113.10': 'like', '198.51.100.77': 'angry'}


def test_untrusted_proxy_still_collapses_but_check_warns(settings):
    """Without TRUSTED_PROXY_CIDRS the collapse is unavoidable (XFF is
    untrustworthy), so a deployment check must flag it instead of failing
    silently."""
    from blog.checks import W_NO_TRUSTED_PROXIES, check_trusted_proxy_cidrs

    settings.DEBUG = False
    settings.TRUSTED_PROXY_CIDRS = []
    warnings = check_trusted_proxy_cidrs(None)
    assert [w.id for w in warnings] == [W_NO_TRUSTED_PROXIES]

    settings.TRUSTED_PROXY_CIDRS = ['172.16.0.0/12']
    assert check_trusted_proxy_cidrs(None) == []


# ---------------------------------------------------------------------------
# GET /api/blog/reactions/mine/ — browser-side read of one's own reactions
# ---------------------------------------------------------------------------

def test_my_reactions_returns_only_callers_reactions(api_client, post, settings):
    settings.TRUSTED_PROXY_CIDRS = []
    api_client.post(f'/api/blog/reactions/{post.id}/', {'reaction': 'love'},
                    REMOTE_ADDR='203.0.113.10')
    # Someone else's reaction on the same post must never be reported as mine.
    Reaction.objects.create(post=post, ip_address='198.51.100.77', reaction='angry')

    response = api_client.get(f'/api/blog/reactions/mine/?post_ids={post.id}',
                              REMOTE_ADDR='203.0.113.10')
    assert response.status_code == 200
    assert response.data == {str(post.id): 'love'}

    # A different visitor sees only their own.
    response = api_client.get(f'/api/blog/reactions/mine/?post_ids={post.id}',
                              REMOTE_ADDR='198.51.100.77')
    assert response.data == {str(post.id): 'angry'}

    # A visitor without any reaction gets an empty map (never someone else's).
    response = api_client.get(f'/api/blog/reactions/mine/?post_ids={post.id}',
                              REMOTE_ADDR='192.0.2.5')
    assert response.data == {}


def test_my_reactions_ignores_soft_removed_and_bad_input(api_client, post, settings):
    settings.TRUSTED_PROXY_CIDRS = []
    Reaction.objects.create(post=post, ip_address='203.0.113.10', reaction=None)

    response = api_client.get(f'/api/blog/reactions/mine/?post_ids={post.id}',
                              REMOTE_ADDR='203.0.113.10')
    assert response.data == {}  # soft-removed reaction is not "mine"

    for query in ('', '?post_ids=', '?post_ids=abc,,-1'):
        response = api_client.get(f'/api/blog/reactions/mine/{query}',
                                  REMOTE_ADDR='203.0.113.10')
        assert response.status_code == 200
        assert response.data == {}
