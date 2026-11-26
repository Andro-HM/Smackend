import re


def canonical_username(value):
    return value.strip().lower()


def canonical_email(value):
    return value.strip().lower()


def is_e164(value):
    return re.fullmatch(r"[+][1-9][0-9]{6,14}", value) is not None
