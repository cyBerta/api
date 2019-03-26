"""
These are tests for the private API endpoints that are relevant to OONI
Explorer.
"""

country_query = {
    'probe_cc': 'IT'
}

country_network_query = {
    'probe_cc': 'IT',
    'probe_asn': 12874
}

def test_api_private_countries(client):
    response = client.get("/api/_/countries")
    assert response.status_code == 200

def test_api_private_global_overview(client):
    response = client.get("/api/_/global_overview")
    assert response.status_code == 200

def test_api_private_test_coverage(client):
    response = client.get("/api/_/test_coverage",
                          query_string={"probe_cc":"IT"})
    assert response.status_code == 200

def test_api_private_test_coverage_test_groups(client):
    response = client.get("/api/_/test_coverage",
                          query_string={
                              "probe_cc":"CA",
                              "test_groups": "websites,performance"
                           })
    assert response.status_code == 200

def test_api_private_country_overview(client):
    response = client.get("/api/_/country_overview",
                          query_string={"probe_cc": "US"})
    assert response.status_code == 200

def test_api_private_website_networks(client):
    response = client.get("/api/_/website_networks",
                          query_string={"probe_cc": "US"})
    assert response.status_code == 200

def test_api_private_website_urls(client):
    response = client.get("/api/_/website_urls",
                          query_string={
                              "probe_cc": "IT",
                              "probe_asn": 12874
                          })
    assert response.status_code == 200

def test_api_private_website_stats(client):
    response = client.get("/api/_/website_stats",
                          query_string={
                              "probe_cc": "IT",
                              "probe_asn": 12874,
                              "input": "http://demonoid.ph"
                          })
    assert response.status_code == 200

def test_api_private_im_networks(client):
    response = client.get("/api/_/im_networks",
                          query_params={
                              "probe_cc": "IT"
                          })
    assert response.status_code == 200

def test_api_private_im_stats(client):
    response = client.get("/api/_/im_stats",
                          query_string={
                              "probe_cc": "IT",
                              "probe_asn": 12874
                          })
    assert response.status_code == 200

def test_api_private_network_stats(client):
    response = client.get("/api/_/network_stats",
                          query_string={"probe_cc": "GR"})
    assert response.status_code == 200
