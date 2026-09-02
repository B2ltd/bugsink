from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0005_messagingserviceconfig_alert_triggers"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagingserviceconfig",
            name="alert_on_resolve",
            field=models.BooleanField(
                default=False,
                help_text="Send when an issue is resolved (useful to close a linked GitHub issue)",
            ),
        ),
    ]
