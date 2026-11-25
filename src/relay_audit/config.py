"""Local settings and trust boundary validation."""

from dataclasses import dataclass, field, fields
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Profile:
    name: str = "default"
    relay_url: str = ""
    relay_key: str = field(default="", repr=False)
    relay_auth: str = "x-api-key"
    local_token: str = field(default="", repr=False)
    aws_profile: str | None = None
    aws_account: str = ""
    aws_region: str = ""
    log_group: str = ""
    allowed_roles: tuple[str, ...] = ()
    model_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    profile_models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sample_basis_points: int = 500
    log_wait: int = 180
    clock_skew: int = 5
    max_body_bytes: int = 4 * 1024 * 1024
    max_log_pages: int = 50
    max_concurrency: int = 16
    max_queue: int = 1000
    retention_days: int = 7
    upstream_timeout: float = 120.0
    upstream_request_id_header: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str = ""
    port: int = 8765
    state_dir: Path = Path(".relay-audit")


def load_profile(path: Path, name: str) -> Profile:
    with path.open("rb") as handle:
        profiles = tomllib.load(handle).get("profiles", {})
    if name not in profiles:
        raise ValueError(f"Unknown profile: {name}")
    raw = dict(profiles[name])
    state_path = Path(raw.pop("state_dir", ".relay-audit")).expanduser()
    credentials = {}
    for key, default in (("relay_key", "RELAY_AUDIT_RELAY_KEY"), ("local_token", "RELAY_AUDIT_LOCAL_TOKEN")):
        variable = raw.pop(key + "_env", default)
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise ValueError(f"Invalid environment variable name for {key}")
        credentials[key] = os.environ.get(variable, "")
    allowed = {item.name for item in fields(Profile)} - {"name", "state_dir", *credentials}
    if unknown := raw.keys() - allowed:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    for key in ("model_map", "profile_models"):
        mapping = raw.get(key, {})
        if not isinstance(mapping, dict) or any(
            not isinstance(values, list) or not values or
            any(not isinstance(value, str) or not value for value in values)
            for values in mapping.values()
        ):
            raise ValueError(f"Invalid {key}")
        raw[key] = {alias: tuple(values) for alias, values in mapping.items()}
    roles = raw.get("allowed_roles", [])
    if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
        raise ValueError("Invalid allowed_roles")
    raw["allowed_roles"] = tuple(roles)
    if not state_path.is_absolute():
        state_path = path.resolve().parent / state_path
    profile = Profile(name=name, state_dir=state_path, **credentials, **raw)
    validate(profile)
    return profile


def validate(profile: Profile) -> None:
    parsed = urlsplit(profile.relay_url)
    local_test = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1")
    if (parsed.scheme != "https" and not local_test) or not parsed.hostname or (
        parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ValueError("relay_url must be a fixed HTTPS base URL or loopback HTTP")
    if parsed.path.rstrip("/").endswith("/v1"):
        raise ValueError("relay_url must omit the /v1 suffix")
    if profile.relay_auth not in {"bearer", "x-api-key"}:
        raise ValueError("Invalid relay_auth")
    if not re.fullmatch(r"[0-9]{12}", profile.aws_account) or not re.fullmatch(
        r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", profile.aws_region
    ) or not profile.log_group:
        raise ValueError("AWS account, region and dedicated log group are required")
    if not profile.allowed_roles or any(
        not re.fullmatch(rf"arn:aws(?:-[a-z]+)*:iam::{profile.aws_account}:(?:role|user)/[\w+=,.@/-]+", arn)
        for arn in profile.allowed_roles
    ):
        raise ValueError("Configure exact IAM role or user ARNs in the evidence account")
    if profile.upstream_request_id_header and not re.fullmatch(
        r"[A-Za-z0-9-]+", profile.upstream_request_id_header
    ):
        raise ValueError("Invalid request ID header")
    if profile.s3_bucket and (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", profile.s3_bucket)
        or not profile.s3_prefix.strip("/")
    ):
        raise ValueError("Large-body S3 requires a dedicated bucket and nonempty key prefix")
    for key, minimum, maximum in (
        ("sample_basis_points", 0, 10000), ("log_wait", 0, 3600), ("clock_skew", 0, 120),
        ("port", 1024, 65535), ("max_body_bytes", 1024, 32 * 1024**2), ("max_log_pages", 1, 500),
        ("max_concurrency", 1, 128), ("max_queue", 1, 100000), ("retention_days", 1, 365),
    ):
        value = getattr(profile, key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Invalid {key}")
    if type(profile.upstream_timeout) not in (int, float) or not 1 <= profile.upstream_timeout <= 900:
        raise ValueError("Invalid upstream_timeout")


def require_relay(profile: Profile, *, local: bool = False) -> None:
    if not profile.relay_key:
        raise ValueError("Set the configured relay key environment variable")
    if local and len(profile.local_token) < 24:
        raise ValueError("Set the local token environment variable to at least 24 characters")
