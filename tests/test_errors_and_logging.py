import json
import logging
from pathlib import Path

import httpx
from conftest import PASSWORD, READ, RESTFUL_SECRET, SESSION_ID, WRITE, WRITE_KEY

from gateway.config import Settings
from gateway.logs import secret_registry
from gateway.main import create_app


def test_unknown_route_returns_message(client):
    response = client.get("/v1/nope", headers=READ)
    assert response.status_code == 404
    assert response.json() == {"message": "Not Found"}


def test_validation_errors_do_not_echo_input(client):
    response = client.post(
        "/v1/users",
        json={"userName": "", "password": "Leaky-P@ssw0rd!", "unexpected": "Leaky-P@ssw0rd!"},
        headers=WRITE,
    )
    assert response.status_code == 400
    assert set(response.json()) == {"message"}
    assert "Leaky-P@ssw0rd!" not in response.text


def test_invalid_session_id_is_400(client):
    response = client.get("/v1/sessions/not-a-guid", headers=READ)
    assert response.status_code == 400


def test_upstream_errors_map_to_message(client, fake):
    def timeout(args):
        raise httpx.ConnectTimeout("down")

    fake.restful["SendMessageToSession"] = timeout
    response = client.post(f"/v1/sessions/{SESSION_ID}/messages", json={"from": "IT", "message": "hi"}, headers=WRITE)
    assert response.status_code == 502
    assert response.json() == {"message": "cannot connect to restful upstream"}


def test_backend_header_present_when_no_upstream_involved(client):
    assert client.get("/v1/companies").headers["X-Gateway-Backend"] == "none"


def test_secrets_are_redacted_from_any_log(caplog):
    create_app(Settings(read_key="a-read-key-123", write_key=WRITE_KEY, restful_secret=RESTFUL_SECRET, sc_password=PASSWORD))
    with caplog.at_level(logging.INFO):
        logging.getLogger("anything").info("secret=%s key=%s pw=%s", RESTFUL_SECRET, WRITE_KEY, PASSWORD)
    assert RESTFUL_SECRET not in caplog.text
    assert WRITE_KEY not in caplog.text
    assert PASSWORD not in caplog.text
    assert "secret=*** key=*** pw=***" in caplog.text


def test_native_session_material_is_registered_for_redaction(client, fake, caplog):
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []
    client.get("/v1/session-groups", headers=READ)
    assert secret_registry.redact(f"token {fake.calls[0][3].headers['x-anti-forgery-token']}") == "token ***"
    assert secret_registry.redact(f"cookie {fake.valid_cookie}") == "cookie ***"


def test_audit_log_for_writes_without_sensitive_content(client, fake, caplog):
    fake.restful["SendCommandToSession"] = lambda args: None
    fake.native["SecurityService.ashx/SaveUser"] = lambda args: None
    with caplog.at_level(logging.INFO):
        client.post(f"/v1/sessions/{SESSION_ID}/commands", json={"command": "net user secret-cmd"}, headers=WRITE)
        client.post("/v1/users", json={"userName": "u1", "password": "Pw-Never-Logged-1"}, headers=WRITE)
        client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"}, headers=READ)
        client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)

    audit = [json.loads(r.getMessage()) for r in caplog.records if r.name == "gateway.audit"]
    assert len(audit) == 3  # writes only
    command, user, denied = audit
    assert command["endpoint"] == "POST /v1/sessions/{sessionId}/commands"
    assert command["sessionId"] == SESSION_ID
    assert command["keyId"] == "write"
    assert command["backend"] == "restful"
    assert command["result"] == "ok" and command["status"] == 202
    assert set(command) == {"time", "endpoint", "sessionId", "clientIp", "keyId", "backend", "status", "result"}
    assert command["clientIp"] == "127.0.0.1"
    assert user["endpoint"] == "POST /v1/users" and user["backend"] == "native"
    assert denied["keyId"] == "read" and denied["status"] == 403 and denied["result"] == "error"
    assert "secret-cmd" not in caplog.text
    assert "Pw-Never-Logged-1" not in caplog.text


def test_committed_openapi_matches_generated():
    app = create_app(Settings(read_key="r-key-123456", write_key="w-key-123456"))
    committed = json.loads((Path(__file__).parent.parent / "openapi.json").read_text())
    assert committed == app.openapi(), "run: uv run python scripts/export_openapi.py"


def test_openapi_contract(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    assert spec["servers"] == [{"url": "https://{hostname}", "variables": {"hostname": {"default": "example.com"}}}]
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"] == {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
    assert spec["security"] == [{"ApiKeyAuth": []}]
    assert spec["paths"]["/health"]["get"]["security"] == []
    operation_ids = {op["operationId"] for item in spec["paths"].values() for op in item.values()}
    assert operation_ids == {
        "health", "list_companies", "list_company_sessions", "list_session_groups", "list_group_sessions",
        "search_sessions", "get_session", "get_session_details", "get_connection_logs", "create_session",
        "send_command", "send_message", "add_note", "run_toolbox_item", "rename_session",
        "update_custom_properties", "create_user",
    }
    for item in spec["paths"].values():
        for op in item.values():
            assert "422" not in op["responses"]
            for param in op.get("parameters", []):
                if param["name"] == "backend":
                    assert param.get("required", False) is False
    assert spec["paths"]["/v1/sessions/{sessionId}/commands"]["post"]["requestBody"]["content"]["application/json"]["schema"]


def test_tracebacks_are_redacted(caplog):
    create_app(Settings(read_key="a-read-key-123", write_key=WRITE_KEY, restful_secret=RESTFUL_SECRET))
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError(f"upstream said {RESTFUL_SECRET}")
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    assert RESTFUL_SECRET not in caplog.text
    assert "upstream said ***" in caplog.text


def test_keyless_audit_lines_are_capped(client, caplog):
    with caplog.at_level(logging.INFO):
        for _ in range(70):
            client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"})
    assert len([r for r in caplog.records if r.name == "gateway.audit"]) == 60


def test_docker_lockfile_matches_uv_lock():
    import shutil
    import subprocess

    import pytest

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    root = Path(__file__).parent.parent
    expected = subprocess.run(
        [uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--no-header"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    committed = (root / "requirements.lock").read_text()
    assert expected.strip() in committed, "regenerate requirements.lock (see README)"
