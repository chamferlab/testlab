from django.db import migrations


def add_units(apps, schema_editor):
    TestUnit = apps.get_model("planning", "TestUnit")
    for order, code in enumerate("ABCD", start=1):
        TestUnit.objects.get_or_create(code=code, defaults={"sort_order": order})


class Migration(migrations.Migration):
    """The lab's four test units; further units and names are added in the admin."""

    dependencies = [("planning", "0001_lab_planning")]

    operations = [migrations.RunPython(add_units, migrations.RunPython.noop)]
