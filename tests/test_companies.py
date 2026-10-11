from conftest import READ, live_data, raw_session

SPACED = " Contoso Orthopedics"
QUOTED = "O'Brien's Bakery"


def summaries():
    return [
        [{"Name": "All Machines by Company", "SessionCount": 5, "HasSubgroupExpression": True}],
        [
            {"Name": "Zeta Corp", "SessionCount": 2, "HasSubgroupExpression": False},
            {"Name": SPACED, "SessionCount": 1, "HasSubgroupExpression": False},
            {"Name": QUOTED, "SessionCount": 2, "HasSubgroupExpression": False},
            {"Name": "   ", "SessionCount": 4, "HasSubgroupExpression": False},
        ],
        [],
    ]


def test_native_companies_use_last_non_empty_level(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data(summaries=summaries())
    response = client.get("/v1/companies", headers=READ)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "native"
    assert response.json() == [
        {"id": SPACED, "name": SPACED.strip(), "sessionCount": 1},
        {"id": QUOTED, "name": QUOTED, "sessionCount": 2},
        {"id": "Zeta Corp", "name": "Zeta Corp", "sessionCount": 2},
    ]
    request = fake.calls_to("native", "PageService.ashx/GetLiveData")[0]
    assert request == [
        {
            "HostSessionInfo": {
                "sessionType": 2,
                "sessionGroupPathParts": ["All Machines by Company"],
                "filter": "",
                "findSessionID": "",
                "sessionLimit": 1,
            },
            "ActionCenterInfo": {},
        },
        0,
    ]


def test_restful_companies_group_by_custom_property_1(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: [
        raw_session("a", properties=["Acme"]),
        raw_session("b", properties=["Acme"]),
        raw_session("c", properties=[" Acme"]),
        raw_session("d", properties=["Beta"], session_type=0),  # support sessions excluded
        raw_session("e", properties=[""]),
    ]
    response = client.get("/v1/companies", headers=READ)
    assert response.headers["X-Gateway-Backend"] == "restful"
    assert fake.calls_to("restful", "GetSessionsByFilter") == [["CustomProperty1 <> ''"]]
    assert response.json() == [
        {"id": " Acme", "name": "Acme", "sessionCount": 1},
        {"id": "Acme", "name": "Acme", "sessionCount": 2},
    ]


def test_companies_are_cached(client, fake, clock):
    fake.restful["GetSessionsByFilter"] = lambda args: [raw_session(properties=["Acme"])]
    client.get("/v1/companies", headers=READ)
    client.get("/v1/companies", headers=READ)
    assert len(fake.calls_to("restful", "GetSessionsByFilter")) == 1
    clock.now += 301
    client.get("/v1/companies", headers=READ)
    assert len(fake.calls_to("restful", "GetSessionsByFilter")) == 2


def test_company_sessions_escape_quotes_and_match_exactly(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: [
        raw_session("a", properties=[QUOTED, "Main", "", "Server"]),
        raw_session("b", properties=[QUOTED + " "]),
    ]
    response = client.get(f"/v1/companies/{QUOTED}/sessions", headers=READ)
    assert response.status_code == 200
    assert fake.calls_to("restful", "GetSessionsByFilter") == [["CustomProperty1 = 'O''Brien''s Bakery'"]]
    sessions = response.json()
    assert [s["sessionId"] for s in sessions] == ["a"]
    assert sessions[0]["company"] == QUOTED
    assert sessions[0]["deviceType"] == "Server"
    assert sessions[0]["customProperties"] == [QUOTED, "Main", "", "Server", "", "", "", ""]


def test_company_id_with_leading_space_round_trips(make_client, fake):
    client = make_client(backend_preference="native-first")
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live_data(
        [raw_session("a", properties=[SPACED]), raw_session("b", properties=[SPACED.strip()])]
    )
    response = client.get("/v1/companies/%20Contoso%20Orthopedics/sessions", headers=READ)
    assert response.status_code == 200
    assert [s["sessionId"] for s in response.json()] == ["a"]
    request = fake.calls_to("native", "PageService.ashx/GetLiveData")[0][0]["HostSessionInfo"]
    assert request["sessionGroupPathParts"] == ["All Machines by Company", SPACED]


def test_company_id_containing_slash(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: [raw_session(properties=["A/B Corp"])]
    response = client.get("/v1/companies/A%2FB%20Corp/sessions", headers=READ)
    assert response.status_code == 200
    assert fake.calls_to("restful", "GetSessionsByFilter") == [["CustomProperty1 = 'A/B Corp'"]]


def test_unknown_company_is_404(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: []
    response = client.get("/v1/companies/Nobody/sessions", headers=READ)
    assert response.status_code == 404
    assert response.json() == {"message": "no sessions found for company 'Nobody'"}
