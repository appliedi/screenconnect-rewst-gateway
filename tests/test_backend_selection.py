import httpx
from conftest import READ, SESSION_ID, WRITE, live_data, raw_session


def test_restful_first_by_default(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session()]
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "restful"
    assert response.json()["isOnline"] is None


def test_native_first_preference(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session(online=True)])
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.headers["X-Gateway-Backend"] == "native"
    assert response.json()["isOnline"] is True
    assert fake.calls_to("restful", "GetSessionBySessionID") == []


def test_read_retries_once_then_falls_back_on_5xx(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: httpx.Response(500)
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session()])
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "native"
    assert len(fake.calls_to("restful", "GetSessionBySessionID")) == 2  # original + one retry
    request = fake.calls_to("native", "PageService.ashx/GetLiveData")[0]
    assert request[0]["HostSessionInfo"]["findSessionID"] == SESSION_ID


def test_read_falls_back_on_timeout(client, fake):
    def timeout(args):
        raise httpx.ReadTimeout("slow")

    fake.restful["GetSessionBySessionID"] = timeout
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session()])
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.headers["X-Gateway-Backend"] == "native"


def test_no_fallback_on_upstream_4xx(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: httpx.Response(400)
    response = client.get("/v1/sessions", params={"filter": "bad ((("}, headers=READ)
    assert response.status_code == 400
    assert response.headers["X-Gateway-Backend"] == "restful"
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []
    assert len(fake.calls_to("restful", "GetSessionsByFilter")) == 1


def test_not_found_does_not_fall_back(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: []
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.status_code == 404
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []


def test_both_backends_fail_returns_last_error(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: httpx.Response(503)
    fake.native["PageService.ashx/GetLiveData"] = lambda args: httpx.Response(500)
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.status_code == 502
    assert response.headers["X-Gateway-Backend"] == "native"
    assert "message" in response.json()


def test_forced_backend_disables_fallback(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: httpx.Response(500)
    response = client.get(f"/v1/sessions/{SESSION_ID}?backend=restful", headers=READ)
    assert response.status_code == 502
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []


def test_forced_unsupported_backend_is_400(client):
    response = client.post(
        f"/v1/sessions/{SESSION_ID}/notes?backend=native", json={"note": "x"}, headers=WRITE
    )
    assert response.status_code == 400


def test_disabled_backend_is_skipped(make_client, fake):
    client = make_client(restful_secret="")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session()])
    response = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ)
    assert response.headers["X-Gateway-Backend"] == "native"
    assert fake.calls_to("restful", "GetSessionBySessionID") == []


def test_only_endpoint_with_disabled_backend_is_503(make_client):
    client = make_client(restful_secret="")
    response = client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "x"}, headers=WRITE)
    assert response.status_code == 503
    assert response.json() == {"message": "restful backend is not configured"}


def test_health_reports_disabled_backend(make_client, fake):
    client = make_client(sc_password="")
    fake.restful["GetSessionsByName"] = lambda args: []
    body = client.get("/health").json()
    assert body["native"] == {"ok": False, "enabled": False, "error": "not configured"}
    assert body["status"] == "ok"


def test_write_never_retried_and_no_fallback_on_5xx(client, fake):
    fake.restful["SendCommandToSession"] = lambda args: httpx.Response(500)
    response = client.post(f"/v1/sessions/{SESSION_ID}/commands", json={"command": "whoami"}, headers=WRITE)
    assert response.status_code == 502
    assert len(fake.calls_to("restful", "SendCommandToSession")) == 1
    assert fake.calls_to("native", "PageService.ashx/AddSessionEvents") == []


def test_write_no_fallback_on_timeout(client, fake):
    def timeout(args):
        raise httpx.ReadTimeout("slow")

    fake.restful["SendCommandToSession"] = timeout
    response = client.post(f"/v1/sessions/{SESSION_ID}/commands", json={"command": "whoami"}, headers=WRITE)
    assert response.status_code == 504
    assert fake.calls_to("native", "PageService.ashx/AddSessionEvents") == []


def test_write_falls_back_on_auth_failure(client, fake):
    fake.restful["SendCommandToSession"] = lambda args: httpx.Response(401)
    fake.native["PageService.ashx/AddSessionEvents"] = lambda args: None
    response = client.post(
        f"/v1/sessions/{SESSION_ID}/commands",
        json={"command": "Get-Process", "shell": "powershell", "timeoutMs": 60000, "group": "Servers"},
        headers=WRITE,
    )
    assert response.status_code == 202
    assert response.json() == {"sessionId": SESSION_ID, "queued": True}
    assert response.headers["X-Gateway-Backend"] == "native"
    assert fake.calls_to("native", "PageService.ashx/AddSessionEvents") == [
        [["Servers"], [{"SessionID": SESSION_ID, "EventType": 44, "Data": "#!ps\n#timeout=60000\nGet-Process"}]]
    ]
