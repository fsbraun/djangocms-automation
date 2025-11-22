from django.db import models

from cms.models.fields import PlaceholderRelationField


class Automation(models.Model):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AutomationContent(models.Model):
    automation = models.ForeignKey(Automation, related_name="contents", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    body = models.TextField()

    placeholders = PlaceholderRelationField()

    def get_title(self, lang):
        return self.title

    def __str__(self):
        return self.title

    def get_placeholder_slots(self):
        return ["hi"]
