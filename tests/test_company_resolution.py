from urllib.parse import quote

from conftest import READ, live_data, raw_session

from gateway.normalize import normalize_company_id, normalize_session

COMPANIES = ["Contoso Health", " Fabrikam Labs ", "O'Brien's Bakery", "Acme", "ACME ", "Northwind  Traders"]


def all_sessions():
    return [raw_session(f"id-{index}", properties=[company]) for index, company in enumerate(COMPANIES)]


def setup(fake):
    fake.restful["GetSessionsByFilter"] = lambda args: all_sessions()
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data(all_sessions())


def last_filter(fake):
    return fake.calls_to("restful", "GetSessionsByFilter")[-1]


def test_normalize_company_id():
    assert normalize_company_id("  Zeta,  Corp ") == "zeta, corp"
    assert normalize_company_id("O'Brien's") == "o'brien's"


def test_exact_match_is_used_as_is(client, fake):
    setup(fake)
    response = client.get("/v1/companies/Contoso%20Health/sessions", headers=READ)
    assert response.status_code == 200
    assert "X-Gateway-Resolved-Company" not in response.headers
    assert last_filter(fake) == ["CustomProperty1 = 'Contoso Health'"]


def test_case_only_difference_resolves(client, fake):
    setup(fake)
    response = client.get("/v1/companies/contoso%20health/sessions", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Resolved-Company"] == quote("Contoso Health", safe="")
    assert [s["company"] for s in response.json()] == ["Contoso Health"]
    assert last_filter(fake) == ["CustomProperty1 = 'Contoso Health'"]


def test_trimmed_id_resolves_to_raw_value_with_spaces(client, fake):
    setup(fake)
    response = client.get("/v1/companies/fabrikam%20labs/sessions", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Resolved-Company"] == quote(" Fabrikam Labs ", safe="")
    assert [s["company"] for s in response.json()] == [" Fabrikam Labs "]


def test_apostrophe_resolves_and_stays_escaped(client, fake):
    setup(fake)
    response = client.get("/v1/companies/o%27brien%27s%20bakery/sessions", headers=READ)
    assert response.status_code == 200
    assert last_filter(fake) == ["CustomProperty1 = 'O''Brien''s Bakery'"]


def test_internal_whitespace_collapses(client, fake):
    setup(fake)
    response = client.get("/v1/companies/northwind%20traders/sessions", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Resolved-Company"] == quote("Northwind  Traders", safe="")


def test_ambiguous_id_is_409_with_candidates(client, fake):
    setup(fake)
    response = client.get("/v1/companies/acme/sessions", headers=READ)
    assert response.status_code == 409
    assert response.json() == {"message": "ambiguous company id", "candidates": ["ACME ", "Acme"]}


def test_exact_match_wins_over_ambiguity(client, fake):
    setup(fake)
    response = client.get("/v1/companies/Acme/sessions", headers=READ)
    assert response.status_code == 200
    assert [s["company"] for s in response.json()] == ["Acme"]


def test_unknown_company_still_404(client, fake):
    setup(fake)
    response = client.get("/v1/companies/nobody%20inc/sessions", headers=READ)
    assert response.status_code == 404
    assert response.json() == {"message": "no sessions found for company 'nobody inc'"}


def test_group_sessions_resolve_company_segment(client, fake):
    setup(fake)
    response = client.get(
        "/v1/session-groups/sessions",
        params=[("path", "All Machines by Company"), ("path", "fabrikam labs")],
        headers=READ,
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Resolved-Company"] == quote(" Fabrikam Labs ", safe="")
    info = fake.calls_to("native", "PageService.ashx/GetLiveData")[-1][0]["HostSessionInfo"]
    assert info["sessionGroupPathParts"] == ["All Machines by Company", " Fabrikam Labs "]


def test_restful_guest_info_is_mapped():
    raw = {
        "SessionID": "s1",
        "CustomPropertyValues": ["Acme"],
        "GuestInfo": {"MachineName": "PC-7", "LoggedOnUserName": "jdoe", "OperatingSystemName": "Windows 11 Pro"},
        "ActiveConnections": [{"ProcessType": 2}],
    }
    session = normalize_session(raw)
    assert (session["guestMachineName"], session["guestLoggedOnUserName"], session["operatingSystem"]) == (
        "PC-7",
        "jdoe",
        "Windows 11 Pro",
    )
    assert session["isOnline"] is True
    assert normalize_session({"SessionID": "s2"})["isOnline"] is None
