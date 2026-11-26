from uuid import UUID

from django.http import FileResponse
from ninja import File, Form, Router, Schema, UploadedFile
from ninja.pagination import paginate

from smackend.errors import APIError
from smackend.identity.permissions import session_auth

from .service import delete_file, open_file, store_files, visible_files


class FileInfo(Schema):
    id: UUID
    name: str
    content_type: str
    public: bool
    size: int

    @staticmethod
    def resolve_size(asset):
        return asset.blob.size


def media_router():
    router = Router(tags=["Files"], auth=session_auth)

    @router.get("/", response=list[FileInfo])
    @paginate
    def list_files(request):
        return visible_files(request.user).filter(owner=request.user)

    @router.post("/", response={201: list[FileInfo]})
    def upload(request, files: File[list[UploadedFile]], public: Form[bool] = False):
        if getattr(request, "smackend_upload_rejected", False):
            raise APIError(413, "upload_limit", "The upload exceeds the allowed size or count.")
        return 201, store_files(request.user, files, public=public)

    @router.post("/{asset_id}/replace", response=FileInfo)
    def replace(request, asset_id: UUID, file: File[UploadedFile], public: Form[bool] = False):
        if getattr(request, "smackend_upload_rejected", False):
            raise APIError(413, "upload_limit", "The upload exceeds the allowed size or count.")
        return store_files(request.user, [file], public=public, replace=asset_id)[0]

    @router.get("/{asset_id}", auth=None, response=FileInfo)
    def metadata(request, asset_id: UUID):
        asset = visible_files(request.user).filter(pk=asset_id).first()
        if asset is None:
            raise APIError(404, "not_found", "File not found.")
        return asset

    @router.get("/{asset_id}/download", auth=None)
    def download(request, asset_id: UUID):
        asset = metadata(request, asset_id)
        response = FileResponse(open_file(asset), as_attachment=True, filename=asset.name,
                                content_type="application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @router.delete("/{asset_id}", response={204: None})
    def remove(request, asset_id: UUID):
        delete_file(request.user, asset_id)
        return 204, None

    return router
