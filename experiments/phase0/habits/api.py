from typing import Annotated

from django.db.models import F
from ninja import Router, Schema, Status
from ninja.errors import HttpError
from pydantic import ConfigDict, StringConstraints

from .models import Habit

router = Router()


class HabitInput(Schema):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class HabitOutput(Schema):
    id: int
    label: str
    repetitions: int


@router.get("/", response=list[HabitOutput])
def list_habits(request):
    return list(Habit.objects.filter(owner=request.auth).order_by("id")[:100])


@router.post("/", response={201: HabitOutput})
def create_habit(request, payload: HabitInput):
    return Status(201, Habit.objects.create(owner=request.auth, label=payload.label))


@router.post("/{habit_id}/repeat", response={204: None})
def repeat_habit(request, habit_id: int):
    updated = Habit.objects.filter(id=habit_id, owner=request.auth).update(repetitions=F("repetitions") + 1)
    if not updated:
        raise HttpError(404, "Habit not found")
    return Status(204, None)
