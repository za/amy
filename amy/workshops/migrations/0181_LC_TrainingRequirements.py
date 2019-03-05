# Generated by Django 2.1.7 on 2019-03-03 20:06

from django.db import migrations


def add_LC_training_requirements(apps, schema_editor):
    """Add LC-related training requirements (homework and demo)."""

    TrainingRequirement = apps.get_model('workshops', 'TrainingRequirement')
    TrainingRequirement.objects.create(name='LC Demo', url_required=False,
                                       event_required=False)
    TrainingRequirement.objects.create(name='LC Homework', url_required=True,
                                       event_required=False)


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0180_trainingrequest_underrepresented'),
    ]

    operations = [
        migrations.RunPython(add_LC_training_requirements),
    ]
