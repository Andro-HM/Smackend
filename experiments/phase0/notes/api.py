from typing import Annotated

from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja import Router, Schema, Status
from pydantic import ConfigDict, StringConstraints

from .models import Note

router = Router()


class NoteInput(Schema):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    body: Annotated[str, StringConstraints(max_length=4000)] = ""


class NoteOutput(Schema):
    id: int
    title: str
    body: str


def owned_notes(request):
    return Note.objects.filter(owner=request.auth)


@router.get("/", response=list[NoteOutput])
def list_notes(request):
    return list(owned_notes(request).order_by("-id")[:100])


@router.post("/", response={201: NoteOutput})
def create_note(request, payload: NoteInput):
    return Status(201, Note.objects.create(owner=request.auth, **payload.model_dump()))


@router.get("/{note_id}", response=NoteOutput)
def read_note(request, note_id: int):
    return get_object_or_404(owned_notes(request), id=note_id)


@router.put("/{note_id}", response=NoteOutput)
@transaction.atomic
def update_note(request, note_id: int, payload: NoteInput):
    note = get_object_or_404(owned_notes(request).select_for_update(), id=note_id)
    note.title, note.body = payload.title, payload.body
    note.save(update_fields=["title", "body"])
    return note


@router.delete("/{note_id}", response={204: None})
def delete_note(request, note_id: int):
    get_object_or_404(owned_notes(request), id=note_id).delete()
    return Status(204, None)
