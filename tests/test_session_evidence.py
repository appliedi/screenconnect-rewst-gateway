"""Identity/activity evidence fields, checked through API responses on both backends."""

from conftest import FIXED_NOW, READ, SESSION_ID

EVIDENCE_FIELDS = {
    "guestMachineSerialNumber",
    "guestHardwareNetworkAddress",
    "guestClientVersion",
    "guestInfoUpdateTime",
    "lastGuestConnectedEventTime",
    "lastGuestDisconnectedEventTime",
    "isDeleted",
    "isEnded",
}
NATIVE_NEVER = -1.7976931348623157e308  # what GetLiveData sends for "never"


def restful_session(**overrides):
    """Shape verified against the live RESTful API Manager payload."""
    session = {
        "SessionID": SESSION_ID,
        "SessionType": 2,
        "Name": "ACME-LT-042",
        "IsPublic": False,
        "Code": "",
        "CustomPropertyValues": ["Acme", "HQ", "", "Laptop", "", "", "", ""],
        "GuestInfo": {
            "MachineName": "ACME-LT-042",
            "MachineSerialNumber": "5CD1234XYZ",
            "HardwareNetworkAddress": "00:1A:2B:3C:4D:5E",
            "OperatingSystemName": "Microsoft Windows 11 Pro",
            "LoggedOnUserName": "",
        },
        "GuestClientVersion": "25.4.16.9293",
        "GuestInfoUpdateTime": "2026-10-11T11:58:12.1234567Z",
        "LastGuestConnectedEventTime": "2026-10-04T08:15:00.123456Z",
        "LastGuestDisconnectedEventTime": "2026-10-03T22:01:00.7654321Z",
        "IsDeleted": False,
        "IsEnded": False,
        "ActiveConnections": [{"ProcessType": 2}],
        "LogonSessions": [],
    }
    session.update(overrides)
    return session


def native_live_session(**overrides):
    """Shape verified against live GetLiveData: times are seconds before the response."""
    session = {
        "SessionID": SESSION_ID,
        "SessionType": 2,
        "Name": "ACME-LT-042",
        "CustomPropertyValues": ["Acme", "HQ", "", "Laptop", "", "", "", ""],
        "GuestOperatingSystemName": "Microsoft Windows 11 Pro",
        "GuestClientVersion": "25.4.16.9293",
        "LastGuestDisconnectedEventTime": 3600.0,
        "LastConnectedEventTime": 60.0,  # includes host connections: must not become a guest time
        "ActiveConnections": [{"ProcessType": 1}],  # host only
        "LogonSessions": [],
    }
    session.update(overrides)
    return session


def native_details():
    """Shape verified against live GetSessionDetails: no SessionID/Name/custom properties."""
    base_ms = FIXED_NOW.timestamp() * 1000
    return {
        "BaseTime": base_ms,
        "Session": {
            "GuestMachineSerialNumber": "5CD1234XYZ",
            "GuestHardwareNetworkAddress": "00:1A:2B:3C:4D:5E",
            "GuestInfoUpdateTime": 120.0,
            "GuestMachineName": "ACME-LT-042",
        },
        "Events": [{"EventID": "e1", "EventType": 44, "Time": 90_000.0, "Data": "whoami"}],  # ms before BaseTime
        "Connections": [],
    }


def live(sessions, untruncated=None):
    info = {"Sessions": sessions, "PathSessionGroupSummaries": []}
    if untruncated is not None:
        info["UntruncatedSessionCount"] = untruncated
    return {"ResponseInfoMap": {"HostSessionInfo": info}}


# ---------- restful ----------


def test_restful_get_session_exposes_all_evidence(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [restful_session()]
    body = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).json()
    assert EVIDENCE_FIELDS <= set(body)
    assert body["guestMachineSerialNumber"] == "5CD1234XYZ"
    assert body["guestHardwareNetworkAddress"] == "00:1A:2B:3C:4D:5E"
    assert body["guestClientVersion"] == "25.4.16.9293"
    assert body["guestInfoUpdateTime"] == "2026-10-11T11:58:12.123456Z"
    assert body["lastGuestConnectedEventTime"] == "2026-10-04T08:15:00.123456Z"
    assert body["lastGuestDisconnectedEventTime"] == "2026-10-03T22:01:00.765432Z"
    assert body["isDeleted"] is False and body["isEnded"] is False  # false is preserved, not nulled
    assert body["isOnline"] is True


def test_restful_missing_null_and_sentinel_values_become_null(client, fake):
    sparse = restful_session(
        GuestInfo=None,
        GuestClientVersion=None,
        GuestInfoUpdateTime="garbage",
        LastGuestConnectedEventTime="0001-01-01T00:00:00",  # ScreenConnect's "never"
        LastGuestDisconnectedEventTime="2026-10-01T00:00:00",  # no timezone: ambiguous
        ActiveConnections=[],
    )
    del sparse["IsDeleted"], sparse["IsEnded"]
    blank_serial = restful_session(
        SessionID="22222222-2222-2222-2222-222222222222",
        GuestInfo={"MachineSerialNumber": " ", "HardwareNetworkAddress": ""},
    )
    fake.restful["GetSessionsByFilter"] = lambda args: [sparse, blank_serial]
    response = client.get("/v1/sessions", params={"filter": "SessionType = 'Access'"}, headers=READ)
    first, second = response.json()
    for field in EVIDENCE_FIELDS:
        assert first[field] is None, field
    assert first["isOnline"] is False
    assert second["guestMachineSerialNumber"] is None and second["guestHardwareNetworkAddress"] is None
    assert response.headers["X-Gateway-Truncated"] == "unknown"


def test_offline_session_keeps_recent_activity(client, fake):
    offline = restful_session(ActiveConnections=[])
    fake.restful["GetSessionsByFilter"] = lambda args: [offline]
    body = client.get("/v1/companies/Acme/sessions", headers=READ).json()[0]
    assert body["isOnline"] is False
    assert body["lastGuestDisconnectedEventTime"] == "2026-10-03T22:01:00.765432Z"


def test_host_only_connection_is_not_online(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [restful_session(ActiveConnections=[{"ProcessType": 1}])]
    assert client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).json()["isOnline"] is False


def test_restful_details_nested_session_has_evidence(client, fake):
    fake.restful["GetSessionDetailsBySessionID"] = lambda args: {"Session": restful_session(), "Events": []}
    session = client.get(f"/v1/sessions/{SESSION_ID}/details", headers=READ).json()["session"]
    assert session["guestMachineSerialNumber"] == "5CD1234XYZ"
    assert session["isEnded"] is False


# ---------- native ----------


def test_native_list_uses_only_list_evidence_and_no_per_session_calls(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([native_live_session()], untruncated=1)
    response = client.get("/v1/companies/Acme/sessions", headers=READ)
    body = response.json()[0]
    assert response.headers["X-Gateway-Backend"] == "native"
    assert response.headers["X-Gateway-Truncated"] == "false"
    assert body["guestClientVersion"] == "25.4.16.9293"
    assert body["lastGuestDisconnectedEventTime"] == "2026-10-11T11:00:00Z"  # FIXED_NOW - 3600 s
    assert body["lastGuestConnectedEventTime"] is None  # LastConnectedEventTime counts hosts: not mapped
    assert body["isOnline"] is False  # host-only connection
    for field in ("guestMachineSerialNumber", "guestHardwareNetworkAddress", "guestInfoUpdateTime", "isDeleted", "isEnded"):
        assert body[field] is None, field
    assert fake.calls_to("native", "PageService.ashx/GetSessionDetails") == []


def test_native_never_sentinel_is_null(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live(
        [native_live_session(LastGuestDisconnectedEventTime=NATIVE_NEVER)]
    )
    body = client.get(f"/v1/sessions?name=ACME-LT-042", headers=READ).json()[0]
    assert body["lastGuestDisconnectedEventTime"] is None


def test_native_get_session_adds_detail_evidence(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([native_live_session()])
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: native_details()
    body = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).json()
    assert body["guestMachineSerialNumber"] == "5CD1234XYZ"
    assert body["guestHardwareNetworkAddress"] == "00:1A:2B:3C:4D:5E"
    assert body["guestInfoUpdateTime"] == "2026-10-11T11:58:00Z"  # BaseTime - 120 s
    assert body["sessionId"] == SESSION_ID and body["company"] == "Acme"
    assert len(fake.calls_to("native", "PageService.ashx/GetSessionDetails")) == 1


def test_native_get_session_without_details_returns_nulls(make_client, fake):
    import httpx

    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([native_live_session()])
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: httpx.Response(500)
    response = client.get(f"/v1/sessions/{SESSION_ID}?backend=native", headers=READ)
    assert response.status_code == 200
    assert response.json()["guestMachineSerialNumber"] is None
    assert response.json()["guestClientVersion"] == "25.4.16.9293"


def test_native_details_session_keeps_identity(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([native_live_session()])
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: native_details()
    body = client.get(f"/v1/sessions/{SESSION_ID}/details", headers=READ).json()
    assert body["session"]["sessionId"] == SESSION_ID
    assert body["session"]["company"] == "Acme"
    assert body["session"]["guestMachineSerialNumber"] == "5CD1234XYZ"
    assert body["events"] == [
        {
            "eventId": "e1",
            "eventType": 44,
            "time": "2026-10-11T11:58:30Z",
            "host": None,
            "data": "whoami",
            "connectionId": None,
            "correlationEventId": None,
        }
    ]


def test_native_group_listing_reports_truncation(client, fake):
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([native_live_session()], untruncated=500)
    response = client.get("/v1/session-groups/sessions", params={"path": "All Machines", "limit": 1}, headers=READ)
    assert response.headers["X-Gateway-Truncated"] == "true"
    assert EVIDENCE_FIELDS <= set(response.json()["sessions"][0])


def test_openapi_documents_evidence_fields(client):
    session = client.get("/openapi.json").json()["components"]["schemas"]["Session"]["properties"]
    assert EVIDENCE_FIELDS <= set(session)
    for field in EVIDENCE_FIELDS:
        assert {"type": "null"} in session[field]["anyOf"], field
