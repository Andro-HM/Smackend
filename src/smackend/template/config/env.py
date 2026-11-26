"""Read project environment values and validate the inputs used by Django."""

from ipaddress import ip_address
import os
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv.parser import parse_stream

DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
ORIGIN_AUTHORITY = re.compile(r"(?:\[[a-f0-9:.]+\]|[a-z0-9.-]+)(?::[0-9]{1,5})?", re.IGNORECASE | re.ASCII)


def read_environment(path):
    """Read literal dotenv values, then overlay the process environment."""
    values = {}
    try:
        with path.open(encoding="utf-8-sig") as source:
            for entry in parse_stream(source):
                if entry.error or (entry.key is not None and entry.value is None):
                    raise ImproperlyConfigured(
                        f"Invalid .env assignment on line {entry.original.line}. Use NAME=value."
                    )
                if entry.key is not None:
                    values[entry.key] = entry.value
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        raise ImproperlyConfigured("Could not read .env. Use a readable UTF-8 file.") from None
    return {**values, **os.environ}


def required(values, name):
    value = values.get(name, "")
    if not value.strip():
        raise ImproperlyConfigured(f"Set {name} in the environment or .env.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ImproperlyConfigured(f"{name} must be a single line without control characters.")
    return value


def choice(values, name, default, options):
    value = values.get(name, default)
    if value not in options:
        raise ImproperlyConfigured(f"{name} must be one of {', '.join(options)}.")
    return value


def boolean(values, name, default="false"):
    return choice(values, name, default, ("true", "false", "1", "0")) in {"true", "1"}


def port(values, name, default):
    value = values.get(name, default)
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise ImproperlyConfigured(f"{name} must be a port from 1 to 65535.")
    return int(value)


def integer(values, name, default, minimum, maximum):
    value = values.get(name, str(default))
    if not (value.isascii() and value.isdecimal() and len(value) <= len(str(maximum))):
        raise ImproperlyConfigured(f"{name} must be an integer from {minimum} to {maximum}.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ImproperlyConfigured(f"{name} must be an integer from {minimum} to {maximum}.")
    return parsed


def host(value, *, http):
    """Accept exact DNS names and IP addresses. Django expects bracketed IPv6."""
    bracketed = value.startswith("[") and value.endswith("]")
    address = value[1:-1] if bracketed else value
    if "%" in address:
        raise ValueError("Scoped addresses are unsupported")
    try:
        ip = ip_address(address)
    except ValueError:
        name = value.lower()
        if (
            len(name) > 253
            or re.fullmatch(r"[0-9.]+", name)
            or not all(DNS_LABEL.fullmatch(label) for label in name.split("."))
        ):
            raise ValueError("Invalid host") from None
        return name
    if (ip.version == 6 and http) != bracketed:
        raise ValueError("Invalid address brackets")
    return f"[{ip.compressed}]" if bracketed else ip.compressed


def database_host(value):
    try:
        return host(value, http=False)
    except ValueError:
        raise ImproperlyConfigured("POSTGRES_HOST must be a hostname or IP address without a port.") from None


def allowed_hosts(value, *, production):
    try:
        hosts = [host(item.strip(), http=True) for item in value.split(",")]
    except ValueError:
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS must contain comma-separated exact hostnames or IP addresses without ports."
        ) from None
    if production:
        for name in hosts:
            if name == "localhost" or name.endswith(".localhost"):
                raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS cannot contain loopback hosts in production.")
            try:
                ip = ip_address(name.strip("[]"))
            except ValueError:
                continue
            ip = getattr(ip, "ipv4_mapped", None) or ip
            if ip.is_loopback or ip.is_unspecified:
                raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS cannot contain loopback or unspecified IPs in production.")
    return list(dict.fromkeys(hosts))


def trusted_origins(value, *, production):
    if not value.strip():
        return []
    origins = []
    for item in value.split(","):
        origin = item.strip()
        try:
            if any(char.isspace() or ord(char) < 32 for char in origin) or "?" in origin or "#" in origin:
                raise ValueError("Invalid origin")
            url = urlsplit(origin)
            schemes = {"https"} if production else {"http", "https"}
            if (
                url.scheme not in schemes or not url.hostname or url.path
                or not ORIGIN_AUTHORITY.fullmatch(url.netloc)
            ):
                raise ValueError("Invalid origin")
            hostname = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
            authority = host(hostname, http=True)
            origin_port = url.port
            if origin_port == 0:
                raise ValueError("Invalid port")
            if origin_port is not None and origin_port != {"http": 80, "https": 443}[url.scheme]:
                authority += f":{origin_port}"
            origins.append(f"{url.scheme}://{authority}")
        except ValueError:
            scheme_hint = "HTTPS" if production else "HTTP or HTTPS"
            raise ImproperlyConfigured(
                f"DJANGO_CSRF_TRUSTED_ORIGINS must contain comma-separated {scheme_hint} origins "
                "with exact hosts and without credentials, paths, queries or fragments."
            ) from None
    return list(dict.fromkeys(origins))
