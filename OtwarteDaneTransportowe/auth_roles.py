from rest_framework.permissions import SAFE_METHODS, BasePermission


ROLE_ADMIN = 'Admin'
ROLE_BLOGGER = 'Blogger'
ROLE_EDITOR = 'Editor'
ROLE_DATA_PROVIDER = 'DataProvider'
ROLE_HELPER = 'Helper'

ALLOWED_ROLE_NAMES = frozenset({
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_EDITOR,
    ROLE_DATA_PROVIDER,
    ROLE_HELPER,
})


def is_admin(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and (
            user.is_superuser
            or user.is_staff
            or user.groups.filter(name=ROLE_ADMIN).exists()
        )
    )


def has_role(user, role: str) -> bool:
    if is_admin(user):
        return True
    return bool(user and user.is_authenticated and user.groups.filter(name=role).exists())


def can_publish_blog(user) -> bool:
    return has_role(user, ROLE_BLOGGER)

def can_edit_any_blog(user) -> bool:
    return has_role(user, ROLE_EDITOR)


def can_add_feeds(user) -> bool:
    return has_role(user, ROLE_DATA_PROVIDER)


def can_confirm_feeds(user) -> bool:
    return has_role(user, ROLE_HELPER)


def has_admin_role(user) -> bool:
    """True when the user has the Django group ``Admin`` (role), not merely staff/superuser."""
    return bool(
        user
        and user.is_authenticated
        and user.groups.filter(name=ROLE_ADMIN).exists()
    )


def is_helper_reviewer(user) -> bool:
    """Helper group without Admin role — confirmation queue only, no feed content edits."""
    return bool(
        user
        and user.is_authenticated
        and user.groups.filter(name=ROLE_HELPER).exists()
        and not has_admin_role(user)
    )


def _is_submission_owner_provider(user, submission) -> bool:
    return (
        submission.submitted_by_id == user.id
        and user.groups.filter(name=ROLE_DATA_PROVIDER).exists()
    )


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
