from django.db import migrations


GROUP_NAMES = ['Admin', 'Blogger', 'Editor', 'DataProvider', 'Helper']


def create_role_groups(apps, schema_editor):
    group_model = apps.get_model('auth', 'Group')
    for name in GROUP_NAMES:
        group_model.objects.get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('cases', '0003_delete_publictransport'),
    ]

    operations = [
        migrations.RunPython(create_role_groups, migrations.RunPython.noop),
    ]
