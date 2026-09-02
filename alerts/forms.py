from django.forms import ModelForm

from .models import MessagingServiceConfig

_TRIGGER_FIELDS = ["alert_on_new", "alert_on_regression", "alert_on_unmute", "alert_on_merge", "alert_on_resolve"]


class MessagingServiceConfigNewForm(ModelForm):

    def __init__(self, project, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["alert_on_new"].label = "Alert on new issues"
        self.fields["alert_on_regression"].label = "Alert on regressions"
        self.fields["alert_on_unmute"].label = "Alert on unmutes"
        self.fields["alert_on_merge"].label = "Alert on merges"
        self.fields["alert_on_resolve"].label = "Alert on resolves"

    class Meta:
        model = MessagingServiceConfig
        fields = ["display_name", "kind", *_TRIGGER_FIELDS]

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.project = self.project
        if commit:
            instance.save()
        return instance


class MessagingServiceConfigEditForm(ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["alert_on_new"].label = "Alert on new issues"
        self.fields["alert_on_regression"].label = "Alert on regressions"
        self.fields["alert_on_unmute"].label = "Alert on unmutes"
        self.fields["alert_on_merge"].label = "Alert on merges"
        self.fields["alert_on_resolve"].label = "Alert on resolves"

    class Meta:
        model = MessagingServiceConfig
        fields = ["display_name", *_TRIGGER_FIELDS]
