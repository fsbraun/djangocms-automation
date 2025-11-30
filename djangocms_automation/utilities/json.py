from typing import Any

from django.db import models
from django.db.models.query import QuerySet
from datetime import datetime, date, time


def cleaned_data_to_json_serializable(cleaned_data: dict[str, Any]) -> dict[str, Any]:
    """
    Convert form.cleaned_data into a JSON-serializable dict.
    Rules:
    - datetime/date/time -> ISO 8601 string
    - model instance -> key + '_id' with its pk
    - iterable of model instances -> key + '_ids' with list of pks
    - QuerySet -> key + '_ids'
    - other basic types kept as-is
    - unknown complex objects -> str() fallback
    """
    out: dict[str, Any] = {}
    for key, value in cleaned_data.items():
        if isinstance(value, (datetime, date, time)):
            out[key] = value.isoformat()
        elif isinstance(value, models.Model):
            out[f"{key}_id"] = value.pk
        elif isinstance(value, QuerySet):
            out[f"{key}_ids"] = [obj.pk for obj in value]
        elif isinstance(value, (list, tuple, set)):
            # Only treat as model instance collection if non-empty and all members are models
            if value and all(isinstance(v, models.Model) for v in value):
                out[f"{key}_ids"] = [v.pk for v in value]
            else:
                converted = []
                for v in value:
                    if isinstance(v, (datetime, date, time)):
                        converted.append(v.isoformat())
                    elif isinstance(v, models.Model):
                        converted.append(v.pk)
                    elif isinstance(v, (str, int, float, bool)) or v is None:
                        converted.append(v)
                    else:
                        converted.append(str(v))
                out[key] = converted
        elif isinstance(value, dict):
            sub: dict[str, Any] = {}
            for sk, sv in value.items():
                if isinstance(sv, (datetime, date, time)):
                    sub[sk] = sv.isoformat()
                elif isinstance(sv, models.Model):
                    sub[f"{sk}_id"] = sv.pk
                elif isinstance(sv, (str, int, float, bool)) or sv is None:
                    sub[sk] = sv
                else:
                    sub[sk] = str(sv)
            out[key] = sub
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out
