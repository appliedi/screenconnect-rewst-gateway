from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

BackendPreference = Literal["restful-first", "native-first"]
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    sc_base_url: str = ""
    restful_secret: str = ""
    sc_username: str = ""
    sc_password: str = ""
    sc_totp_secret: str = ""
    read_key: str = ""
    write_key: str = ""
    backend_preference: BackendPreference = "restful-first"
    company_group_path: str = "All Machines by Company"
    company_cache_seconds: int = 300
    allowed_source_ips: tuple[IPNetwork, ...] = field(default_factory=tuple)
    port: int = 8080
    native_default_group: str = "All Machines"
    upstream_timeout_seconds: float = 30.0
    health_cache_seconds: int = 15
    request_deadline_seconds: float = 90.0
    allowed_user_roles: tuple[str, ...] = ()

    @property
    def restful_enabled(self) -> bool:
        return bool(self.sc_base_url and self.restful_secret)

    @property
    def native_enabled(self) -> bool:
        return bool(self.sc_base_url and self.sc_username and self.sc_password)

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [
            value
            for value in (
                self.restful_secret,
                self.sc_password,
                self.sc_totp_secret,
                self.read_key,
                self.write_key,
            )
            if value
        ]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            value = env.get(name, default).strip()
            # docker --env-file passes quotes through literally
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value

        preference = get("BACKEND_PREFERENCE", "restful-first").lower()
        if preference not in ("restful-first", "native-first"):
            raise ConfigError("BACKEND_PREFERENCE must be 'restful-first' or 'native-first'")

        read_key = get("GATEWAY_READ_KEY")
        write_key = get("GATEWAY_WRITE_KEY")
        if not write_key and not read_key:
            raise ConfigError("Set GATEWAY_WRITE_KEY and/or GATEWAY_READ_KEY")
        if read_key and read_key == write_key:
            raise ConfigError("GATEWAY_READ_KEY and GATEWAY_WRITE_KEY must differ")

        base_url = get("SC_BASE_URL").rstrip("/")
        if base_url and not base_url.startswith("https://"):
            raise ConfigError("SC_BASE_URL must start with https://")

        try:
            networks = tuple(
                ipaddress.ip_network(cidr.strip(), strict=False)
                for cidr in get("ALLOWED_SOURCE_IPS").split(",")
                if cidr.strip()
            )
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_SOURCE_IPS: {exc}") from exc

        return cls(
            sc_base_url=base_url,
            restful_secret=get("SC_RESTFUL_SECRET"),
            sc_username=get("SC_USERNAME"),
            sc_password=env.get("SC_PASSWORD", ""),
            sc_totp_secret=get("SC_TOTP_SECRET").replace(" ", "").upper(),
            read_key=read_key,
            write_key=write_key,
            backend_preference=preference,  # type: ignore[arg-type]
            company_group_path=get("COMPANY_GROUP_PATH", "All Machines by Company"),
            company_cache_seconds=int(get("COMPANY_CACHE_SECONDS", "300")),
            allowed_source_ips=networks,
            port=int(get("PORT", "8080")),
            native_default_group=get("NATIVE_DEFAULT_GROUP", "All Machines"),
            allowed_user_roles=tuple(role.strip() for role in get("ALLOWED_USER_ROLES").split(",") if role.strip()),
        )
