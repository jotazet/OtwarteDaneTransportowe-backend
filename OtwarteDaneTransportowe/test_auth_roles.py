"""Role resolution: the ``Admin`` group (or superuser) is the only source of truth."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_HELPER,
    can_add_feeds,
    can_confirm_feeds,
    can_manage_cases,
    can_publish_blog,
    has_admin_role,
    is_admin,
    is_helper_reviewer,
    role_names,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


def _user(username, *, staff=False, superuser=False, groups=()):
    user = User.objects.create_user(username, f'{username}@example.org', 'password')
    user.is_staff = staff
    user.is_superuser = superuser
    user.save(update_fields=['is_staff', 'is_superuser'])
    for name in groups:
        group, _ = Group.objects.get_or_create(name=name)
        user.groups.add(group)
    return user


def test_is_staff_alone_grants_no_api_roles():
    """Regression: is_staff used to short-circuit has_role() and hand out everything.

    An account made purely for django-admin access silently gained Helper,
    Blogger, Editor and DataProvider rights in the API, while RequiresAdminGroup
    checked the group — two conflicting definitions of "admin".
    """
    staff = _user('staff-only', staff=True)

    assert not is_admin(staff)
    assert not has_admin_role(staff)
    assert not can_confirm_feeds(staff)
    assert not can_publish_blog(staff)
    assert not can_add_feeds(staff)
    assert not can_manage_cases(staff)


def test_admin_group_grants_every_role():
    admin = _user('group-admin', groups=[ROLE_ADMIN])

    assert is_admin(admin)
    assert has_admin_role(admin)
    assert can_confirm_feeds(admin)
    assert can_publish_blog(admin)
    assert can_add_feeds(admin)


def test_superuser_is_an_implicit_admin_for_role_checks():
    """A superuser satisfies the role helpers...

    ...but NOT has_admin_role() — see test_superuser_does_not_hold_the_admin_role
    for the deliberate asymmetry.
    """
    root = _user('root', superuser=True)

    assert is_admin(root)
    assert can_confirm_feeds(root)
    assert can_publish_blog(root)


def test_superuser_does_not_hold_the_admin_role():
    """has_admin_role() is deliberately stricter than is_admin().

    Editing a published feed's source and managing accounts require the
    application-level Admin group, not the Django superuser flag. Pinned here
    and by test_superuser_without_admin_role_cannot_change_url_at_stage_3.
    """
    root = _user('root-no-group', superuser=True)

    assert is_admin(root)
    assert not has_admin_role(root)


def test_only_the_admin_group_satisfies_both_predicates():
    expected = {
        'staff-flag': (False, False),
        'admin-group': (True, True),
        'super-user': (True, False),
        'helper': (False, False),
        'plain': (False, False),
    }
    users = {
        'staff-flag': _user('staff-flag', staff=True),
        'admin-group': _user('admin-group', groups=[ROLE_ADMIN]),
        'super-user': _user('super-user', superuser=True),
        'helper': _user('helper-only', groups=[ROLE_HELPER]),
        'plain': _user('plain'),
    }
    for label, user in users.items():
        assert (is_admin(user), has_admin_role(user)) == expected[label], label


def test_helper_reviewer_excludes_admins():
    helper = _user('helper', groups=[ROLE_HELPER])
    helper_admin = _user('helper-admin', groups=[ROLE_HELPER, ROLE_ADMIN])

    assert is_helper_reviewer(helper)
    assert not is_helper_reviewer(helper_admin)


def test_role_names_are_cached_on_the_instance(django_assert_num_queries):
    """Permission helpers run several times per request; one query is enough."""
    user = _user('cached', groups=[ROLE_HELPER])
    user = User.objects.get(pk=user.pk)  # fresh instance, empty cache

    with django_assert_num_queries(1):
        role_names(user)
        can_confirm_feeds(user)
        can_add_feeds(user)
        is_admin(user)


def test_role_cache_is_dropped_when_roles_change():
    from accounts.services import set_user_roles

    user = _user('rotating', groups=[ROLE_HELPER])
    assert can_confirm_feeds(user)

    set_user_roles(user, [])
    assert not can_confirm_feeds(user), 'stale role cache survived a group change'


def test_anonymous_user_has_no_roles():
    from django.contrib.auth.models import AnonymousUser

    anon = AnonymousUser()
    assert role_names(anon) == frozenset()
    assert not is_admin(anon)
    assert not can_confirm_feeds(anon)
