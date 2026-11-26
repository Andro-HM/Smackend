"""Checks for the infrastructure assembled by a consuming Django project."""

from django.conf import settings
from django.apps import apps
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


def request_options():
    return {"secure": True, "HTTP_HOST": settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "testserver"}


def api_path(suffix):
    return getattr(settings, "SMACKEND_API_PREFIX", "/api").rstrip("/") + suffix


class HttpContracts(SimpleTestCase):
    def test_liveness_does_not_need_database_access(self):
        reply = self.client.get(api_path("/health"), **request_options())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {"status": "ok"})
        self.assertEqual(reply["Cache-Control"], "no-store")
        self.assertRegex(reply["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_missing_route_has_a_correlated_api_error(self):
        reply = self.client.get(api_path("/__smackend_verify_missing__"), **request_options())
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.json()["code"], "not_found")
        self.assertEqual(reply.json()["request_id"], reply["X-Request-ID"])

    def test_outbound_module_matches_configuration(self):
        providers = getattr(settings, "SMACKEND_OUTBOUND_PROVIDERS", None)
        if getattr(settings, "SMACKEND_HTTP_ENABLED", False):
            from smackend.outbound.client import Provider

            self.assertIsInstance(providers, dict)
            self.assertTrue(all(isinstance(value, Provider) for value in providers.values()))
        else:
            self.assertIsNone(providers)


class DatabaseContract(TestCase):
    def test_readiness_uses_the_postgresql_test_database(self):
        reply = self.client.get(api_path("/ready"), **request_options())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {"status": "ready"})
        self.assertEqual(reply["Cache-Control"], "no-store")

    def test_account_module_matches_configuration(self):
        path = api_path("/account/capabilities")
        reply = self.client.get(path, **request_options())
        if not getattr(settings, "SMACKEND_AUTH_ENABLED", False):
            self.assertEqual(reply.status_code, 404)
            return
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(self.client.get(api_path("/account/profile"), **request_options()).status_code, 401)
        self.assertEqual(self.client.get("/identity/browser/v1/auth/session", **request_options()).status_code, 401)

    def test_storage_module_matches_configuration(self):
        if getattr(settings, "SMACKEND_STORAGE_ENABLED", False):
            from django.core.files.storage import Storage, storages

            self.assertIsInstance(storages["smackend_media"], Storage)
            response = self.client.get(api_path("/files/"), **request_options())
            self.assertEqual(response.status_code, 401)
        else:
            self.assertFalse(apps.is_installed("smackend.media"))

    def test_admin_module_matches_configuration(self):
        if getattr(settings, "SMACKEND_ADMIN_ENABLED", False):
            index = reverse(getattr(settings, "SMACKEND_ADMIN_INDEX", "smackend_admin:index"))
            response = self.client.get(index, **request_options())
            self.assertEqual(response.status_code, 302)
        else:
            self.assertFalse(apps.is_installed("smackend.administration"))

    def test_ai_module_matches_configuration(self):
        if not getattr(settings, "SMACKEND_AI_ENABLED", False):
            self.assertFalse(apps.is_installed("smackend.ai"))
            return
        from smackend.ai.configuration import configured_service

        service = configured_service()
        self.assertTrue(service.available("offline", "text"))
        if settings.SMACKEND_AI_DEMO_MODEL:
            response = self.client.post(api_path("/ai/chat"), {}, content_type="application/json", **request_options())
            self.assertEqual(response.status_code, 401)

    def test_realtime_module_matches_configuration(self):
        if not getattr(settings, "SMACKEND_REALTIME_ENABLED", False):
            self.assertFalse(apps.is_installed("smackend.realtime"))
            return
        from importlib import import_module

        module, name = settings.ASGI_APPLICATION.rsplit(".", 1)
        application = getattr(import_module(module), name)
        self.assertIn("websocket", application.application_mapping)
        self.assertIn("http", application.application_mapping)
        self.assertTrue(settings.SMACKEND_REALTIME_ALLOWED_ORIGINS)
