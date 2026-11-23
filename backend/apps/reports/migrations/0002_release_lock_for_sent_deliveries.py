"""Delivery SENT now completes a request (see DELIVERED_STATUSES): release locks stored under the old rule."""

from django.db import migrations


def release(apps, schema_editor):
    ReportRequest = apps.get_model("reports", "ReportRequest")
    ReportRequest.objects.filter(report_status="generated", delivery_status="sent").exclude(
        active_lock=None
    ).update(active_lock=None)


class Migration(migrations.Migration):
    dependencies = [("reports", "0001_initial")]

    operations = [migrations.RunPython(release, migrations.RunPython.noop)]
