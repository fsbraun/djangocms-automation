"""Minimal domain models for the reference automations to act on."""

from django.db import models


class Article(models.Model):
    """Content the nightly digest reports on."""

    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    is_published = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


class Order(models.Model):
    """A record the webhook automation creates."""

    reference = models.CharField(max_length=64, unique=True)
    email = models.EmailField()
    total = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    status = models.CharField(max_length=32, default="received")
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reference} ({self.status})"
