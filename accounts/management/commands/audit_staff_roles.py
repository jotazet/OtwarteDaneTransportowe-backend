"""List accounts whose API permissions changed when ``is_staff`` stopped granting roles.

``auth_roles.is_admin()`` used to return True for any ``is_staff`` account, and
``has_role()`` short-circuits on it — so a user created purely for django-admin
access silently held Helper, Blogger, Editor and DataProvider rights in the API.
That is gone; the ``Admin`` group (or superuser) is the single source of truth.

Run this BEFORE deploying to see who is affected, and ``--grant-admin`` to give
those accounts the Admin group explicitly if that was the intent all along.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN

User = get_user_model()


class Command(BaseCommand):
    help = 'Report (or fix) staff accounts that lost implicit API roles.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--grant-admin',
            action='store_true',
            help='Add the Admin group to every affected account (writes to the DB).',
        )

    def handle(self, *args, **options):
        affected = (
            User.objects
            .filter(is_staff=True, is_superuser=False)
            .exclude(groups__name=ROLE_ADMIN)
            .order_by('username')
        )

        if not affected.exists():
            self.stdout.write(self.style.SUCCESS(
                'No affected accounts: every is_staff user is a superuser or already in the Admin group.'
            ))
            return

        self.stdout.write(
            'These accounts have is_staff=True but are NOT superusers and are NOT in '
            f'the "{ROLE_ADMIN}" group. They previously held every API role implicitly '
            'and no longer do:'
        )
        for user in affected:
            groups = ', '.join(sorted(user.groups.values_list('name', flat=True))) or '(none)'
            self.stdout.write(f'  - {user.username} (id={user.pk}, active={user.is_active}) groups: {groups}')

        if not options['grant_admin']:
            self.stdout.write('')
            self.stdout.write(
                'Re-run with --grant-admin to add the Admin group to all of them, '
                'or assign the specific roles each account actually needs in the admin.'
            )
            return

        group, _ = Group.objects.get_or_create(name=ROLE_ADMIN)
        count = 0
        for user in affected:
            user.groups.add(group)
            count += 1
        self.stdout.write(self.style.SUCCESS(f'Granted the {ROLE_ADMIN} group to {count} account(s).'))
