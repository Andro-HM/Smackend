"""Use Django permissions for broad actions and scoped QuerySets for object access."""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja.security import django_auth

from smackend.errors import APIError


session_auth = django_auth


def require_user(user):
    if not user.is_authenticated or not user.is_active:
        raise APIError(401, "unauthenticated", "Sign in to continue.")
    return user


def require_permission(user, permission, obj=None):
    if not require_user(user).has_perm(permission, obj):
        raise PermissionDenied()


def require_staff(user):
    if not require_user(user).is_staff:
        raise PermissionDenied()


def require_admin(user):
    if not require_user(user).is_superuser:
        raise PermissionDenied()


def owner_scope(queryset, user, *, field="owner"):
    """Use the returned queryset for detail, list, update, delete and aggregates."""
    return queryset.filter(**{field: require_user(user)})


def authorized_scope(queryset, user, action, policy):
    """A consumer policy receives (queryset, authenticated user, action)."""
    return policy(queryset, require_user(user), action)


def authorized_object(queryset, user, action, policy, **lookup):
    return get_object_or_404(authorized_scope(queryset, user, action, policy), **lookup)
