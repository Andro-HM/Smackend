from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class MediaUploadLimit(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        prefix = getattr(settings, "SMACKEND_API_PREFIX", "/api").rstrip("/") + "/files"
        self.managed = request is not None and (request.path_info == prefix or request.path_info.startswith(prefix + "/"))
        self.count = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.count += 1
        if self.managed and self.count > settings.SMACKEND_STORAGE_POLICY.files_per_request:
            self.reject()

    def receive_data_chunk(self, raw_data, start):
        if self.managed and start + len(raw_data) > settings.SMACKEND_STORAGE_POLICY.file_bytes:
            self.reject()
        return raw_data

    def file_complete(self, file_size):
        return None

    def reject(self):
        self.request.smackend_upload_rejected = True
        raise StopUpload(connection_reset=True)
