import asyncio

import httpx
import pyotp
import pytest
from conftest import ANTI_FORGERY, BASE_URL, PASSWORD, READ, RESTFUL_SECRET, SESSION_ID, WRITE, FakeScreenConnect, live_data, raw_session

from gateway.allowlist import ALLOWED_METHODS, NATIVE_METHODS, RESTFUL_METHODS, ensure_allowed
from gateway.errors import UpstreamNotAllowedError
from gateway.normalize import build_command, escape_filter_value
from gateway.upstream.native import NativeClient, extract_anti_forgery_token
from gateway.upstream.restful import EXTENSION_PATH, RestfulClient


def test_allowlist_has_exactly_the_17_documented_methods():
    assert len(RESTFUL_METHODS) == 11
    assert len(NATIVE_METHODS) == 6
    assert sum(len(methods) for methods in ALLOWED_METHODS.values()) == 17


@pytest.mark.parametrize(
    ("backend", "method"),
    [
        ("restful", "DeleteSession"),
        ("restful", "PageService.ashx/GetLiveData"),
        ("native", "SecurityService.ashx/DeleteUser"),
        ("native", "GetSessionsByFilter"),
        ("native", "../PageService.ashx/GetLiveData"),
        ("other", "GetSessionsByFilter"),
    ],
)
def test_allowlist_rejects_everything_else(backend, method):
    with pytest.raises(UpstreamNotAllowedError):
        ensure_allowed(backend, method)


def test_clients_refuse_non_allowlisted_methods():
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json=None))

    async def run():
        restful = RestfulClient(BASE_URL, RESTFUL_SECRET, transport=transport)
        native = NativeClient(BASE_URL, "svc", PASSWORD, transport=transport)
        with pytest.raises(UpstreamNotAllowedError):
            await restful.call("DeleteSession", [SESSION_ID], idempotent=False)
        with pytest.raises(UpstreamNotAllowedError):
            await native.call("SessionService.ashx/DeleteSessions", [], idempotent=False)

    asyncio.run(run())
    assert sent == []


def test_restful_request_shape(client, fake):
    fake.restful["GetSessionsByName"] = lambda args: [raw_session()]
    client.get("/v1/sessions", params={"name": "PC-01"}, headers=READ)
    _, method, args, request = fake.calls[0]
    assert request.method == "POST"
    assert request.url.path == f"{EXTENSION_PATH}/GetSessionsByName"
    assert request.headers["CTRLAuthHeader"] == RESTFUL_SECRET
    assert request.headers["content-type"] == "application/json"
    assert "origin" not in request.headers
    assert args == ["PC-01"]


def test_native_login_without_totp(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: [
        {"Name": "All Machines", "SessionType": 2, "SessionFilter": "", "SubgroupExpressions": ""}
    ]
    response = client.get("/v1/session-groups", headers=READ)
    assert response.status_code == 200
    assert response.json()[0]["name"] == "All Machines"
    assert len(fake.logins) == 1
    user, password, code, persistent, token = fake.logins[0]
    assert (user, password, code, persistent) == ("svc-gateway", PASSWORD, "", True)
    assert isinstance(token, str) and token
    request = fake.calls[0][3]
    assert request.headers["x-anti-forgery-token"] == ANTI_FORGERY
    assert request.headers["origin"] == BASE_URL
    assert request.headers["host"] == "sc.example.com"


def test_native_login_with_totp(make_client, fake):
    secret = pyotp.random_base32()
    client = make_client(sc_totp_secret=secret)
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []
    client.get("/v1/session-groups", headers=READ)
    code = fake.logins[0][2]
    assert len(code) == 6 and pyotp.TOTP(secret).verify(code, valid_window=1)


def test_native_relogin_once_on_expired_session(client, fake, clock):
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []
    assert client.get("/v1/session-groups", headers=READ).status_code == 200
    clock.now += 600
    fake.expire_next_native_call = True
    assert client.get("/v1/session-groups", headers=READ).status_code == 200
    assert len(fake.logins) == 2
    assert len(fake.calls_to("native", "SessionGroupService.ashx/GetSessionGroups")) == 3


def test_native_gives_up_after_one_relogin(client, fake, clock):
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data()
    client.get("/v1/session-groups/sessions?path=x", headers=READ)
    clock.now += 600
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: httpx.Response(403)
    response = client.get("/v1/session-groups", headers=READ)
    assert response.status_code == 502
    assert len(fake.logins) == 2


def test_403_right_after_login_does_not_relogin(client, fake):
    # Permission denied, not an expired session: re-logging in would only reuse the TOTP code.
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: httpx.Response(403)
    assert client.get("/v1/session-groups", headers=READ).status_code == 502
    assert len(fake.logins) == 1


def test_restful_redirect_on_write_is_not_success(client, fake):
    fake.restful["SendCommandToSession"] = lambda args: httpx.Response(302, headers={"location": "/Login"})
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session()])
    fake.native["PageService.ashx/AddSessionEvents"] = lambda args: None
    response = client.post(f"/v1/sessions/{SESSION_ID}/commands", json={"command": "whoami"}, headers=WRITE)
    assert response.status_code == 202
    assert response.headers["X-Gateway-Backend"] == "native"  # redirect means restful never ran it


def test_native_logins_are_serialized():
    fake = FakeScreenConnect()
    fake.native["SessionGroupService.ashx/GetSessionGroups"] = lambda args: []

    async def run():
        native = NativeClient(BASE_URL, "svc", PASSWORD, transport=httpx.MockTransport(fake.handle_native))
        await asyncio.gather(
            *(native.call("SessionGroupService.ashx/GetSessionGroups", [], idempotent=True) for _ in range(10))
        )
        await native.aclose()

    asyncio.run(run())
    assert len(fake.logins) == 1


def test_anti_forgery_extraction_variants():
    assert extract_anti_forgery_token('{"antiForgeryToken":"abc123"}') == "abc123"
    assert extract_anti_forgery_token("SC.context.antiForgeryToken = 'xyz';") == "xyz"
    assert extract_anti_forgery_token("<html>login</html>") is None


def test_login_page_without_token_is_auth_error(make_client, fake):
    original = fake.handle_native

    def no_token(request):
        if request.url.path == "/":
            return httpx.Response(200, text="<html>login</html>")
        return original(request)

    fake.handle_native = no_token  # type: ignore[method-assign]
    client = make_client()
    response = client.get("/v1/session-groups", headers=READ)
    assert response.status_code == 502
    assert "anti-forgery token not found" in response.json()["message"]


def test_escape_and_build_command():
    assert escape_filter_value("O'Brien's Bakery") == "O''Brien''s Bakery"
    assert escape_filter_value("a''b") == "a''''b"
    assert build_command("dir", "cmd", None) == "dir"
    assert build_command("Get-Date", "powershell", None) == "#!ps\nGet-Date"
    assert build_command("dir", "cmd", 5000) == "#timeout=5000\ndir"


def test_session_details_and_events(client, fake):
    fake.restful["GetSessionDetailsBySessionID"] = lambda args: {
        "Session": raw_session(),
        "Events": [
            {"EventID": "e1", "EventType": 44, "Time": "2026-10-11T10:00:00Z", "Data": "whoami"},
            {"EventID": "e2", "EventType": 70, "Time": "2026-10-11T10:00:02Z", "Data": "acme\\svc"},
        ],
    }
    response = client.get(f"/v1/sessions/{SESSION_ID}/details", headers=READ)
    body = response.json()
    assert body["session"]["sessionId"] == SESSION_ID
    assert [e["eventType"] for e in body["events"]] == [44, 70]
    assert body["events"][1] == {
        "eventId": "e2",
        "eventType": 70,
        "time": "2026-10-11T10:00:02Z",
        "host": None,
        "data": "acme\\svc",
        "connectionId": None,
        "correlationEventId": None,
    }


def test_connection_logs_native_with_group(client, fake):
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: {
        "Session": raw_session(),
        "Connections": [
            {"ConnectionID": "c1", "ProcessType": 2, "ParticipantName": "", "ClientType": 1,
             "NetworkAddress": "10.0.0.5", "Events": [{"EventID": "x", "EventType": 10, "Time": "t", "Data": None}]}
        ],
    }
    response = client.get(
        f"/v1/sessions/{SESSION_ID}/connection-logs", params={"group": "All Machines", "backend": "native"}, headers=READ
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "native"
    # group -> sessionGroupPathParts; items 16 = Connections only
    assert fake.calls_to("native", "PageService.ashx/GetSessionDetails") == [[["All Machines"], SESSION_ID, 16]]
    assert response.json()["connections"][0]["networkAddress"] == "10.0.0.5"


def test_group_sessions(client, fake):
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data(
        [raw_session(online=True)],
        [[{"Name": "All Machines by Company", "SessionCount": 3}], [{"Name": "Acme", "SessionCount": 3, "HasSubgroupExpression": False}]],
    )
    response = client.get(
        "/v1/session-groups/sessions",
        params=[("path", "All Machines by Company"), ("path", "Acme"), ("filter", "GuestMachineName LIKE 'PC*'"), ("limit", "50")],
        headers=READ,
    )
    assert response.status_code == 200
    assert response.json()["groups"] == [{"name": "Acme", "sessionCount": 3, "hasSubgroupExpression": False}]
    assert response.json()["sessions"][0]["isOnline"] is True
    info = fake.calls_to("native", "PageService.ashx/GetLiveData")[-1][0]["HostSessionInfo"]
    assert info["sessionGroupPathParts"] == ["All Machines by Company", "Acme"]
    assert info["filter"] == "GuestMachineName LIKE 'PC*'"
    assert info["sessionLimit"] == 50


def test_search_requires_exactly_one_criterion(client):
    assert client.get("/v1/sessions", headers=READ).status_code == 400
    assert client.get("/v1/sessions", params={"name": "a", "filter": "b"}, headers=READ).status_code == 400


def test_native_name_search_uses_text_search_then_exact_match(make_client, fake):
    client = make_client(backend_preference="native-first")
    def by_type(args):
        # Only the Access query finds these sessions, as on a real instance.
        if args[0]["HostSessionInfo"]["sessionType"] != 2:
            return live_data([])
        return live_data([raw_session("a", name="Bob's PC"), raw_session("b", name="Bob's PC 2")])

    fake.native["PageService.ashx/GetLiveData"] = by_type
    response = client.get("/v1/sessions", params={"name": "Bob's PC", "sessionType": 2}, headers=READ)
    assert [s["sessionId"] for s in response.json()] == ["a"]
    info = fake.calls_to("native", "PageService.ashx/GetLiveData")[0][0]["HostSessionInfo"]
    assert info["filter"] == "Bob's PC"


def test_filter_expressions_never_fall_back_to_native(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: httpx.Response(500)
    response = client.get("/v1/sessions", params={"filter": "CustomProperty1 <> ''"}, headers=READ)
    assert response.status_code == 502
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []
    forced = client.get("/v1/sessions", params={"filter": "x", "backend": "native"}, headers=READ)
    assert forced.status_code == 400
    assert forced.json() == {"message": "filter expressions require the restful backend"}


def test_create_user_native(client, fake):
    fake.native["SecurityService.ashx/SaveUser"] = lambda args: None
    response = client.post(
        "/v1/users",
        json={"userName": "tech1", "password": "Sup3r$ecret!", "displayName": "Tech One",
              "email": "t1@example.com", "roles": ["Technician"], "forcePasswordChange": True},
        headers=WRITE,
    )
    assert response.status_code == 201
    assert response.headers["X-Gateway-Backend"] == "native"
    assert fake.calls_to("native", "SecurityService.ashx/SaveUser") == [
        ["InternalMembershipProvider", None, "tech1", "Sup3r$ecret!", "Sup3r$ecret!", None, "Tech One", "",
         "t1@example.com", ["Technician"], True]
    ]


def test_failed_login_cools_down_to_protect_the_service_account(make_client, fake, clock):
    original = fake.handle_native

    def bad_password(request):
        if request.url.path.endswith("/TryLogin"):
            fake.logins.append([])
            return httpx.Response(403)
        return original(request)

    fake.handle_native = bad_password  # type: ignore[method-assign]
    client = make_client()
    for _ in range(5):
        assert client.get("/v1/session-groups", headers=READ).status_code == 502
    assert len(fake.logins) == 1
    clock.now += 61
    client.get("/v1/session-groups", headers=READ)
    assert len(fake.logins) == 2
