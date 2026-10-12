from rest_framework.permissions import SAFE_METHODS, BasePermission

ROLE_ADMIN = 'Admin'
ROLE_BLOGGER = 'Blogger'
ROLE_EDITOR = 'Editor'
ROLE_DATA_PROVIDER = 'DataProvider'
ROLE_HELPER = 'Helper'

ALLOWED_ROLE_NAMES = frozenset(
    {
        ROLE_ADMIN,
        ROLE_BLOGGER,
        ROLE_EDITOR,
        ROLE_DATA_PROVIDER,
        ROLE_HELPER,
    }
)


def role_names(user) -> frozenset:
    """Group names held by ``user``, cached on the instance.

    These helpers are called repeatedly within one request (permission classes,
    get_serializer_class, per-object checks), and each call used to issue its
    own EXISTS query. ``request.user`` lives exactly as long as the request, so
    caching there is safe; :func:`accounts.services.set_user_roles` drops the
    cache when it changes a user's groups.
    """
    if not (user and getattr(user, 'is_authenticated', False)):
        return frozenset()
    cached = getattr(user, '_cached_role_names', None)
    if cached is None:
        cached = frozenset(user.groups.values_list('name', flat=True))
        user._cached_role_names = cached
    return cached


def is_admin(user) -> bool:
    """Admin = the ``Admin`` group, or a Django superuser.

    ``is_staff`` deliberately does NOT grant anything here. It used to, and
    because :func:`has_role` short-circuits on this function, every account
    created merely for django-admin access silently gained Helper, Blogger,
    Editor and DataProvider rights in the API — while ``RequiresAdminGroup``
    and the feed-source edit rules checked the GROUP instead. Two conflicting
    definitions of "admin" coexisted. The group is now the single source of
    truth, with superuser as the only implicit override.

    Note the deliberate asymmetry with :func:`has_admin_role`, which requires
    the group even from a superuser — see its docstring.

    Run ``manage.py audit_staff_roles`` to list accounts affected by this.
    """
    if not (user and getattr(user, 'is_authenticated', False)):
        return False
    return bool(user.is_superuser) or ROLE_ADMIN in role_names(user)


def has_role(user, role: str) -> bool:
    if is_admin(user):
        return True
    return role in role_names(user)


def can_publish_blog(user) -> bool:
    return has_role(user, ROLE_BLOGGER)


def can_edit_any_blog(user) -> bool:
    return has_role(user, ROLE_EDITOR)


def can_add_feeds(user) -> bool:
    return has_role(user, ROLE_DATA_PROVIDER)


def can_confirm_feeds(user) -> bool:
    return has_role(user, ROLE_HELPER)


def has_admin_role(user) -> bool:
    """True only for holders of the ``Admin`` GROUP — a superuser is not enough.

    Deliberately stricter than :func:`is_admin`, and the difference is the point:
    editing a published feed's source (url, credentials, file) and managing user
    accounts require the application-level Admin role, which is granted and
    audited in the app, rather than the Django-level superuser flag handed out
    for shell and admin-site access.
    See ``test_superuser_without_admin_role_cannot_change_url_at_stage_3``.

    The two are allowed to differ; what was NOT allowed — and is now gone — is
    ``is_staff`` silently satisfying is_admin() and handing every role to any
    django-admin account.
    """
    return ROLE_ADMIN in role_names(user)


def is_helper_reviewer(user) -> bool:
    """Helper group without Admin role — confirmation queue only, no feed content edits."""
    return ROLE_HELPER in role_names(user) and not has_admin_role(user)


def _is_submission_owner_provider(user, submission) -> bool:
    return submission.submitted_by_id == user.id and ROLE_DATA_PROVIDER in role_names(user)


def can_edit_static_feed_source(user, submission) -> bool:
    """Whether the user may change static feed source fields (url, file, auth, etc.).

    - Rejected: owner (DataProvider) or Admin role — **not** Helper.
    - Step 1 without rejection: owner or Admin role.
    - Steps 2–4: **only** Admin role (group ``Admin``).
    """
    if not user or not user.is_authenticated:
        return False
    if submission.is_rejected:
        return has_admin_role(user) or _is_submission_owner_provider(user, submission)
    if submission.current_stage < 2:
        return has_admin_role(user) or submission.submitted_by_id == user.id
    if submission.current_stage <= 4:
        return has_admin_role(user)
    return has_admin_role(user)


def can_edit_realtime_submission_content(user, submission) -> bool:
    """Whether the user may change realtime endpoints (url, auth, add/remove), same rules as static."""
    if not user or not user.is_authenticated:
        return False
    if submission.is_rejected:
        return has_admin_role(user) or _is_submission_owner_provider(user, submission)
    if submission.current_stage < 2:
        return has_admin_role(user) or submission.submitted_by_id == user.id
    if submission.current_stage <= 4:
        return has_admin_role(user)
    return has_admin_role(user)


CONFIRMATION_ONLY_PATCH_KEYS = frozenset({'stage', 'rejection_cause'})


def patch_request_includes_submission_content(request) -> bool:
    """True if the body contains anything other than stage / rejection_cause."""
    for key in request.data:
        if key not in CONFIRMATION_ONLY_PATCH_KEYS:
            return True
    return False


def can_manage_cases(user) -> bool:
    return has_role(user, ROLE_HELPER)


class RoleReadOnlyOrWritePermission(BasePermission):
    write_check = staticmethod(lambda user: False)

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return self.write_check(request.user)


class IsBloggerOrReadOnly(RoleReadOnlyOrWritePermission):
    write_check = staticmethod(can_publish_blog)


class IsEditorOrOwnBloggerOrReadOnly(BasePermission):
    """
    Blog permissions:
    - SAFE methods: allow any
    - create: Blogger/Editor/Admin
    - update/partial_update/destroy: Editor/Admin can edit any; Blogger only own posts
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if view.action == 'create':
            return can_publish_blog(user) or can_edit_any_blog(user)
        return can_publish_blog(user) or can_edit_any_blog(user)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if can_edit_any_blog(user):
            return True
        if can_publish_blog(user):
            return getattr(obj, 'author_id', None) == getattr(user, 'id', None)
        return False


class IsCaseManagerOrReadOnly(RoleReadOnlyOrWritePermission):
    write_check = staticmethod(can_manage_cases)


class RequiresAdminGroup(BasePermission):
    """API write/list user management requires Django group ``Admin``."""

    def has_permission(self, request, view):
        return has_admin_role(request.user)


class IsFeedParticipant(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if view.action == 'create':
            return can_add_feeds(user)
        return can_add_feeds(user) or can_confirm_feeds(user)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if can_confirm_feeds(user):
            return True
        return can_add_feeds(user) and obj.submitted_by_id == getattr(user, 'id', None)
