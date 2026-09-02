# Generated manually for alert trigger filters on MessagingServiceConfig

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0004_alter_messagingserviceconfig_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagingserviceconfig",
            name="alert_on_new",
            field=models.BooleanField(
                default=True,
                help_text="Send when a new issue is first seen",
            ),
        ),
        migrations.AddField(
            model_name="messagingserviceconfig",
            name="alert_on_regression",
            field=models.BooleanField(
                default=True,
                help_text="Send when a resolved issue regresses",
            ),
        ),
        migrations.AddField(
            model_name="messagingserviceconfig",
            name="alert_on_unmute",
            field=models.BooleanField(
                default=True,
                help_text="Send when a muted issue is unmuted by volume/time",
            ),
        ),
        migrations.AddField(
            model_name="messagingserviceconfig",
            name="alert_on_merge",
            field=models.BooleanField(
                default=False,
                help_text="Send when issues are manually merged (useful to notify GitHub only after triage)",
            ),
        ),
    ]
