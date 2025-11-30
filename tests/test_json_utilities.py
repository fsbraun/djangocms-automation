"""Tests for cleaned_data_to_json_serializable utility."""

import re
from datetime import date, time

import pytest
from django.utils.timezone import now

from djangocms_automation.utilities.json import cleaned_data_to_json_serializable
from djangocms_automation.models import Automation


@pytest.mark.django_db
def test_basic_types_and_none():
    data = {
        "str": "text",
        "int": 7,
        "float": 3.5,
        "bool": True,
        "none": None,
    }
    out = cleaned_data_to_json_serializable(data)
    assert out == data


@pytest.mark.django_db
def test_datetime_date_time_conversion():
    dt = now()
    d = date.today()
    t = time(10, 5, 1)
    data = {"dt": dt, "d": d, "t": t}
    out = cleaned_data_to_json_serializable(data)
    assert out["dt"].startswith(dt.isoformat()[:19])  # ignore tz tail specifics
    assert out["d"] == d.isoformat()
    assert out["t"] == t.isoformat()


@pytest.mark.django_db
def test_model_instance_and_queryset_and_iterables():
    a1 = Automation.objects.create(name="A1", is_active=True)
    a2 = Automation.objects.create(name="A2", is_active=False)
    qs = Automation.objects.filter(pk__in=[a1.pk, a2.pk])
    data = {
        "single": a1,
        "many_qs": qs,
        "many_list": [a1, a2],
        "many_tuple": (a1, a2),
    }
    out = cleaned_data_to_json_serializable(data)
    assert out["single_id"] == a1.pk
    assert set(out["many_qs_ids"]) == {a1.pk, a2.pk}
    assert out["many_list_ids"] == [a1.pk, a2.pk]
    assert out["many_tuple_ids"] == [a1.pk, a2.pk]


@pytest.mark.django_db
def test_mixed_iterable_conversion():
    a1 = Automation.objects.create(name="A1", is_active=True)
    dt = now()

    class Dummy:
        def __str__(self):
            return "<Dummy>"

    data = {
        "mixed": [a1, "x", 3, 2.5, True, None, dt, Dummy()],
    }
    out = cleaned_data_to_json_serializable(data)
    mixed = out["mixed"]
    # Order should be preserved; check transformations
    assert mixed[0] == a1.pk
    assert mixed[1:][:5] == ["x", 3, 2.5, True, None]
    # datetime converted to iso
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", mixed[6])
    assert mixed[7] == "<Dummy>"


@pytest.mark.django_db
def test_dict_subconversion():
    a1 = Automation.objects.create(name="A1", is_active=True)
    dt = now()
    sub = {"when": dt, "model": a1, "value": 11, "opaque": object()}
    data = {"payload": sub}
    out = cleaned_data_to_json_serializable(data)
    payload = out["payload"]
    assert payload["when"].startswith(dt.isoformat()[:19])
    assert payload["model_id"] == a1.pk
    assert payload["value"] == 11
    assert isinstance(payload["opaque"], str)


@pytest.mark.django_db
def test_unknown_object_fallback():
    class X:
        def __repr__(self):
            return "X()"

    data = {"x": X()}
    out = cleaned_data_to_json_serializable(data)
    assert out["x"] == "X()"


@pytest.mark.django_db
def test_empty_iterables():
    data = {"empty_list": [], "empty_tuple": (), "empty_set": set()}
    out = cleaned_data_to_json_serializable(data)
    assert out["empty_list"] == []
    assert out["empty_tuple"] == []  # tuple converted element-wise, stays list container
    assert out["empty_set"] == []
