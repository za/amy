# Generated by Django 2.0.5 on 2018-06-24 08:56

from django.db import migrations
from django.db.models import Q
from django.db.models.functions import Length

import workshops.fields


def display_wrong_gh_usernames(apps, schema_editor):
    Person = apps.get_model('workshops', 'Person')

    persons = Person.objects.annotate(github_length=Length('github'))

    # filter by too long usernames (>39 characters)
    q1 = Q(github_length__gt=39)
    # filter by usernames that start or end with a hyphen
    q2 = Q(github__startswith='-') | Q(github__endswith='-')
    # filter by usernames that contain more than 1 hyphen
    q3 = Q(github__icontains='--')
    # filter by usernames that don't contain only the allowed characters
    q4 = Q(github__iregex=r'[^a-z0-9-]+')

    persons = persons.filter(q1 | q2 | q3 | q4)
    print("\n")
    print("#" * 20)
    print("People with invalid GitHub usernames:")
    for person in persons:
        print("{} ({} {}):\t{}".format(str(person), person.personal,
                                       person.family, person.github))


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0139_fix_language_names'),
    ]

    operations = [
        migrations.AlterField(
            model_name='person',
            name='github',
            field=workshops.fields.NullableGithubUsernameField(blank=True, default='', help_text='Please put only a single username here.', max_length=39, null=True, unique=True, verbose_name='GitHub username'),
        ),
        migrations.AlterField(
            model_name='profileupdaterequest',
            name='github',
            field=workshops.fields.NullableGithubUsernameField(blank=True, default='', help_text='Please put only a single username here.', max_length=39, null=True, verbose_name='GitHub username'),
        ),
        migrations.AlterField(
            model_name='trainingrequest',
            name='github',
            field=workshops.fields.NullableGithubUsernameField(blank=True, default='', help_text='Please put only a single username here.', max_length=39, null=True, verbose_name='GitHub username'),
        ),
        migrations.RunPython(display_wrong_gh_usernames),
    ]
