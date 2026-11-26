import pytest
from django.core.exceptions import ImproperlyConfigured

from smackend.identity.configuration import read_profile
from smackend.scaffold import create_project


@pytest.mark.parametrize("fields,password,otp,email,phone", [
    ("username!,password!", "username", "", "none", "false"),
    ("email!,password!", "email", "", "mandatory", "false"),
    ("phone!,password!", "phone", "", "none", "true"),
    ("email!", "", "email", "mandatory", "false"),
    ("phone!", "", "phone", "none", "true"),
    ("username!,password!,email,phone", "username", "phone,email", "optional", "false"),
    ("username!,email!,phone!,password!", "username,email,phone", "email,phone", "mandatory", "true"),
])
def test_supported_identity_profiles(fields, password, otp, email, phone):
    profile = read_profile({
        "SMACKEND_AUTH_SIGNUP_FIELDS": fields,
        "SMACKEND_AUTH_PASSWORD_LOGIN": password,
        "SMACKEND_AUTH_OTP": otp,
        "SMACKEND_AUTH_EMAIL_VERIFICATION": email,
        "SMACKEND_AUTH_REQUIRE_PHONE_VERIFICATION": phone,
    })
    assert profile.password_login == tuple(filter(None, password.split(",")))
    assert profile.otp == tuple(filter(None, otp.split(",")))


@pytest.mark.parametrize("overrides", [
    {"SMACKEND_AUTH_SIGNUP_FIELDS": "email!,password!"},
    {"SMACKEND_AUTH_SIGNUP_FIELDS": "username!,username,password!"},
    {"SMACKEND_AUTH_SIGNUP_FIELDS": "username!,password"},
    {"SMACKEND_AUTH_PASSWORD_RESET": "true"},
    {"SMACKEND_AUTH_EMAIL_VERIFICATION": "optional"},
    {"SMACKEND_AUTH_REQUIRE_PHONE_VERIFICATION": "true"},
])
def test_rejects_incompatible_identity_settings(overrides):
    with pytest.raises(ImproperlyConfigured):
        read_profile(overrides)


def test_authentication_is_opt_in(tmp_path):
    off = create_project(tmp_path, "plain")
    on = create_project(tmp_path, "accounts-demo", auth=True)
    assert "SMACKEND_AUTH_ENABLED=false" in (off / ".env").read_text()
    assert "SMACKEND_AUTH_ENABLED=true" in (on / ".env").read_text()
    assert "smackend[backend,auth]" not in (off / "pyproject.toml").read_text()
    assert "smackend[backend,auth]" in (on / "pyproject.toml").read_text()
