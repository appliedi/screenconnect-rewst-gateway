from conftest import READ, SESSION_ID, WRITE, raw_session

from gateway.config import ConfigError, Settings
from gateway.security import identify_key

import pytest


def test_health_needs_no_key(client, fake):
    fake.restful["GetSessionsByName"] = lambda args: []
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "restful": {"ok": True, "enabled": True, "error": None},
        "native": {"ok": True, "enabled": True, "error": None},
    }


def test_missing_and_wrong_key_rejected(client):
    assert client.get("/v1/companies").status_code == 401
    response = client.get("/v1/companies", headers={"X-Api-Key": "nope"})
    assert response.status_code == 401
    assert response.json() == {"message": "missing or invalid X-Api-Key"}


def test_read_key_can_get_but_not_write(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session()]
    assert client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).status_code == 200
    response = client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "hi"}, headers=READ)
    assert response.status_code == 403
    assert fake.calls_to("restful", "AddNoteToSession") == []


def test_write_key_can_do_everything(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session()]
    fake.restful["AddNoteToSession"] = lambda args: None
    assert client.get(f"/v1/sessions/{SESSION_ID}", headers=WRITE).status_code == 200
    response = client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "hi"}, headers=WRITE)
    assert response.status_code == 200
    assert response.json() == {"sessionId": SESSION_ID, "ok": True}


def test_identify_key_uses_constant_time_compare(monkeypatch):
    import gateway.security as security

    compared = []
    real = security.hmac.compare_digest
    monkeypatch.setattr(security.hmac, "compare_digest", lambda a, b: compared.append(b) or real(a, b))
    settings = Settings(read_key="read-1234567", write_key="write-1234567")
    assert identify_key(settings, "write-1234567") == "write"
    assert identify_key(settings, "read-1234567") == "read"
    assert identify_key(settings, "") is None
    assert len(compared) == 6  # both keys compared every time


def test_source_ip_allowlist(make_client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session()]
    fake.restful["GetSessionsByName"] = lambda args: []
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []
    from ipaddress import ip_network

    allowed = make_client(client_addr=("10.1.2.3", 1), allowed_source_ips=(ip_network("10.0.0.0/8"),))
    denied = make_client(client_addr=("192.0.2.9", 1), allowed_source_ips=(ip_network("10.0.0.0/8"),))
    assert allowed.get(f"/v1/sessions/{SESSION_ID}", headers=READ).status_code == 200
    response = denied.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.status_code == 403
    assert response.json() == {"message": "source IP not allowed"}
    assert denied.get("/health").status_code == 200


def test_write_rate_limit(client, fake, clock):
    fake.restful["AddNoteToSession"] = lambda args: None
    for _ in range(10):
        assert client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"}, headers=WRITE).status_code == 200
    response = client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"}, headers=WRITE)
    assert response.status_code == 429
    assert response.json() == {"message": "rate limit exceeded"}
    assert int(response.headers["Retry-After"]) > 0
    clock.now += 61
    assert client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"}, headers=WRITE).status_code == 200


def test_read_rate_limit_is_per_key(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session()]
    for _ in range(120):
        assert client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).status_code == 200
    assert client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).status_code == 429
    assert client.get(f"/v1/sessions/{SESSION_ID}", headers=WRITE).status_code == 200


def test_config_requires_distinct_keys():
    with pytest.raises(ConfigError):
        Settings.from_env({})
    with pytest.raises(ConfigError):
        Settings.from_env({"GATEWAY_READ_KEY": "same-key", "GATEWAY_WRITE_KEY": "same-key"})
    settings = Settings.from_env({"GATEWAY_WRITE_KEY": "w-key", "SC_BASE_URL": "https://x.example.com/"})
    assert settings.sc_base_url == "https://x.example.com"
    assert not settings.restful_enabled and not settings.native_enabled
    quoted = Settings.from_env({"GATEWAY_WRITE_KEY": "w-key", "COMPANY_GROUP_PATH": '"All Machines by Company"'})
    assert quoted.company_group_path == "All Machines by Company"
