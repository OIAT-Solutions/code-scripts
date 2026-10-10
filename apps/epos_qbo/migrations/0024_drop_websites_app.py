"""Drop the retired website-monitoring app (Marvin, 5 Oct 2026: the Wix logs are no longer used).

Its tables (1.1M log events, ~1.9 GB) were most of the database. Frees the space for reuse; run
``VACUUM`` once afterwards to shrink the file. Not reversible.
"""
from django.db import migrations


def drop_websites(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS websites_websitelogevent")
        cur.execute("DROP TABLE IF EXISTS websites_website")
        cur.execute("DELETE FROM django_migrations WHERE app = 'websites'")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="websites").delete()  # cascades its permissions


class Migration(migrations.Migration):
    dependencies = [
        ("epos_qbo", "0023_remove_inventory_review"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(drop_websites, migrations.RunPython.noop)]
