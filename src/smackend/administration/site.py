from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect

from smackend.identity.sessions import revoke_sessions

from .models import AuditEvent, record


class SecureAdminSite(admin.AdminSite):
    site_header = "Administration"

    def has_permission(self, request):
        return super().has_permission(request) and did_recently_reauthenticate(request)

    def login(self, request, extra_context=None):
        if request.method not in {"GET", "HEAD"}:
            return HttpResponseNotAllowed(["GET", "HEAD"])
        if self.has_permission(request):
            return redirect(f"{self.name}:index")
        return redirect("account_frontend")


class ViewOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AccountsAdmin(ViewOnlyAdmin):
    actions = ("activate", "deactivate", "revoke")
    list_display = ("username", "is_active", "is_staff", "is_superuser", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("username", "email")
    list_per_page = 30
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("username", "email", "is_active", "is_superuser", "last_login", "date_joined")

    def get_fields(self, request, obj=None):
        fields = self.readonly_fields
        if request.user.is_superuser:
            fields += ("is_staff", "groups", "user_permissions")
        return fields

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or not obj.is_superuser)

    def get_actions(self, request):
        return super().get_actions(request) if request.user.is_superuser else {}

    def save_model(self, request, obj, form, change):
        if "is_staff" in form.changed_data:
            obj.save(update_fields=["is_staff"])

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if {"groups", "user_permissions", "is_staff"}.intersection(form.changed_data):
            record("roles_changed", form.instance, actor=request.user)
            revoke_sessions(form.instance)

    def update_accounts(self, request, queryset, action, active=None):
        if not request.user.is_superuser:
            raise PermissionDenied()
        with transaction.atomic():
            # Protect the operator and all other superuser accounts.
            users = queryset.filter(is_superuser=False).exclude(pk=request.user.pk)
            count = 0
            for account in users.select_for_update().order_by("pk")[:100]:
                if active is not None:
                    account.is_active = active
                    account.save(update_fields=["is_active"])
                if active is not True:
                    revoke_sessions(account)
                record(action, account, actor=request.user)
                count += 1
        self.message_user(request, f"Updated {count} accounts. Each action handles up to 100 accounts.")

    @admin.action(description="Activate selected accounts")
    def activate(self, request, queryset):
        self.update_accounts(request, queryset, "activated", active=True)

    @admin.action(description="Deactivate selected accounts and revoke sessions")
    def deactivate(self, request, queryset):
        self.update_accounts(request, queryset, "deactivated", active=False)

    @admin.action(description="Revoke selected accounts' sessions")
    def revoke(self, request, queryset):
        self.update_accounts(request, queryset, "sessions_revoked")


class RolesAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    actions = None
    filter_horizontal = ("permissions",)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        record("role_saved", form.instance, actor=request.user)

    def delete_model(self, request, obj):
        record("role_deleted", obj, actor=request.user)
        super().delete_model(request, obj)


class AuditAdmin(ViewOnlyAdmin):
    fields = readonly_fields = ("created_at", "actor", "action", "object_type", "object_id")
    list_display = ("created_at", "actor", "action", "object_type", "object_id")
    list_filter = ("action", "object_type")
    search_fields = ("object_id",)
    list_per_page = 50

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser


class AssetAdmin(ViewOnlyAdmin):
    actions = ("retire",)
    readonly_fields = fields = ("id", "owner", "name", "content_type", "public", "created_at")
    list_display = ("name", "owner", "content_type", "public", "created_at")
    list_filter = ("public", "content_type")
    search_fields = ("name", "owner__username")
    list_per_page = 30

    def get_queryset(self, request):
        rows = super().get_queryset(request)
        return rows if request.user.is_superuser else rows.filter(owner=request.user)

    def get_actions(self, request):
        return super().get_actions(request) if request.user.is_superuser else {}

    @admin.action(description="Retire selected files")
    def retire(self, request, queryset):
        if not request.user.is_superuser:
            raise PermissionDenied()
        from smackend.media.service import retire_asset

        count = 0
        with transaction.atomic():
            rows = list(queryset.order_by("owner_id", "pk").values_list("owner_id", "pk")[:100])
            list(get_user_model().objects.filter(pk__in={owner for owner, _ in rows}).order_by("pk").select_for_update())
            for owner, key in rows:
                asset = self.model.objects.filter(owner_id=owner, pk=key).first()
                if asset:
                    record("file_retired", asset, actor=request.user)
                    retire_asset(asset)
                    count += 1
        self.message_user(request, f"Retired {count} files. Stored objects are queued for cleanup.")


def default_admin_site():
    site = SecureAdminSite(name="smackend_admin")
    site.register(get_user_model(), AccountsAdmin)
    site.register(Group, RolesAdmin)
    site.register(AuditEvent, AuditAdmin)
    if apps.is_installed("smackend.media"):
        site.register(apps.get_model("media", "Asset"), AssetAdmin)
    return site
