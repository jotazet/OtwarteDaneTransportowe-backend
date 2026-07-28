import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from OtwarteDaneTransportowe.auth_roles import ALLOWED_ROLE_NAMES, ROLE_ADMIN

User = get_user_model()


def generate_random_password(user=None) -> str:
    """Return a random password that passes Django validators."""
    for _ in range(20):
        candidate = secrets.token_urlsafe(16)
        try:
            validate_password(candidate, user=user)
        except ValidationError:
            continue
        return candidate
    raise ValidationError('Could not generate a valid random password.')


def user_role_names(user) -> list[str]:
    return sorted(
        name for name in user.groups.values_list('name', flat=True) if name in ALLOWED_ROLE_NAMES
    )


def set_user_roles(user, role_names: list[str]) -> None:
    groups = Group.objects.filter(name__in=role_names)
    user.groups.set(groups)


def admin_group_user_count(exclude_user_id=None) -> int:
    qs = User.objects.filter(groups__name=ROLE_ADMIN, is_active=True).distinct()
    if exclude_user_id is not None:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.count()


def would_remove_last_admin(user, new_roles: list[str] | None, new_is_active: bool | None = None) -> bool:
    """True if the change (role removal, deletion or deactivation) would leave zero active admins.

    ``new_roles is None`` means roles are unchanged; ``new_is_active is False``
    means the user is being deactivated — a deactivated admin no longer counts,
    whatever roles the payload keeps. Deletion callers pass ``(None, None)``.
    """
    if not user.groups.filter(name=ROLE_ADMIN).exists():
        return False
    if new_is_active is False:
        # Deactivation removes this admin from the active pool regardless of roles.
        return admin_group_user_count(exclude_user_id=user.pk) == 0
    if new_roles is None:
        # Roles unchanged: dangerous only for deletion (new_is_active is None);
        # a reactivation (new_is_active is True) never removes an admin.
        return new_is_active is None and admin_group_user_count(exclude_user_id=user.pk) == 0
    if ROLE_ADMIN in new_roles:
        return False
    return admin_group_user_count(exclude_user_id=user.pk) == 0
