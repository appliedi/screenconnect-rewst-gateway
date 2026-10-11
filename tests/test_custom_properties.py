import httpx
import pytest
from conftest import READ, SESSION_ID, WRITE, live_data, raw_session

from gateway.normalize import merge_custom_properties

CURRENT = ["Acme", "HQ", "IT", "Laptop", "e", "f", "g", "h"]


def test_merge_only_touches_supplied_indexes():
    assert merge_custom_properties(CURRENT, {1: "Branch", 6: ""}) == ["Acme", "Branch", "IT", "Laptop", "e", "f", "", "h"]
    assert merge_custom_properties(["x"], {3: "Server"}) == ["x", "", "", "Server", "", "", "", ""]
    with pytest.raises(ValueError):
        merge_custom_properties(CURRENT, {8: "nope"})


def test_restful_reads_merges_and_replaces_all_eight(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session(properties=CURRENT)]
    fake.restful["UpdateSessionCustomProperties"] = lambda args: None
    response = client.patch(
        f"/v1/sessions/{SESSION_ID}/custom-properties",
        json={"site": "Branch", "deviceType": "Workstation", "customProperties": {"5": "tag"}},
        headers=WRITE,
    )
    assert response.status_code == 200
    expected = ["Acme", "Branch", "IT", "Workstation", "e", "tag", "g", "h"]
    assert response.json() == {"sessionId": SESSION_ID, "customProperties": expected}
    assert fake.calls_to("restful", "UpdateSessionCustomProperties") == [[SESSION_ID, expected]]


def test_native_fallback_updates_each_changed_index(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: httpx.Response(401)
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data([raw_session(properties=CURRENT)])
    fake.native["PageService.ashx/UpdateSessionCustomPropertyValue"] = lambda args: None
    response = client.patch(
        f"/v1/sessions/{SESSION_ID}/custom-properties",
        json={"company": "Acme", "department": "Ops", "customProperties": {"7": "z"}},
        headers=WRITE,
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "native"
    assert fake.calls_to("native", "PageService.ashx/UpdateSessionCustomPropertyValue") == [
        [["All Machines"], SESSION_ID, 2, "Ops"],
        [["All Machines"], SESSION_ID, 7, "z"],
    ]


def test_company_change_invalidates_company_cache(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: [raw_session(properties=["Acme"])]
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session(properties=CURRENT)]
    fake.restful["UpdateSessionCustomProperties"] = lambda args: None
    client.get("/v1/companies", headers=READ)
    client.patch(f"/v1/sessions/{SESSION_ID}/custom-properties", json={"site": "x"}, headers=WRITE)
    client.get("/v1/companies", headers=READ)
    assert len(fake.calls_to("restful", "GetSessionsByFilter")) == 1
    client.patch(f"/v1/sessions/{SESSION_ID}/custom-properties", json={"company": "NewCo"}, headers=WRITE)
    client.get("/v1/companies", headers=READ)
    assert len(fake.calls_to("restful", "GetSessionsByFilter")) == 2


@pytest.mark.parametrize(
    "body",
    [{}, {"customProperties": {"8": "x"}}, {"customProperties": {"0": "x"}, "company": "y"}, {"color": "red"}],
)
def test_invalid_custom_property_bodies(client, fake, body):
    response = client.patch(f"/v1/sessions/{SESSION_ID}/custom-properties", json=body, headers=WRITE)
    assert response.status_code == 400
    assert set(response.json()) == {"message"}
    assert fake.calls == []


def test_create_session_maps_named_properties(client, fake):
    fake.restful["CreateSession"] = lambda args: {"SessionID": SESSION_ID, "Name": "New PC"}
    response = client.post(
        "/v1/sessions",
        json={"sessionType": 2, "name": "New PC", "isPublic": False, "code": None, "company": "Acme", "deviceType": "Laptop"},
        headers=WRITE,
    )
    assert response.status_code == 201
    assert response.json() == {"sessionId": SESSION_ID, "name": "New PC", "raw": {"SessionID": SESSION_ID, "Name": "New PC"}}
    assert fake.calls_to("restful", "CreateSession") == [
        [2, "New PC", False, None, ["Acme", "", "", "Laptop", "", "", "", ""]]
    ]


def test_create_user_role_allowlist(make_client, fake):
    fake.native["SecurityService.ashx/SaveUser"] = lambda args: None
    client = make_client(allowed_user_roles=("Technician",))
    ok = client.post("/v1/users", json={"userName": "t1", "password": "Pw-123456!", "roles": ["Technician"]}, headers=WRITE)
    assert ok.status_code == 201
    denied = client.post("/v1/users", json={"userName": "t2", "password": "Pw-123456!", "roles": ["Administrator"]}, headers=WRITE)
    assert denied.status_code == 403
    assert denied.json() == {"message": "roles not allowed: Administrator"}
    assert len(fake.calls_to("native", "SecurityService.ashx/SaveUser")) == 1
