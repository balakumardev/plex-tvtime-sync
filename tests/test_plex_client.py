# tests/test_plex_client.py
import pytest
import responses

from plex_tvtime_sync.plex_client import PlexClient, PlexNotFound

BASE = "http://plex.example:32400"

HISTORY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="3">
  <Video historyKey="/status/sessions/history/1" ratingKey="101" key="/library/metadata/101"
         title="Pilot" grandparentTitle="Some Show" type="episode" viewedAt="2000" accountID="1"/>
  <Video historyKey="/status/sessions/history/2" ratingKey="202" key="/library/metadata/202"
         title="Some Movie" type="movie" viewedAt="3000" accountID="1"/>
  <Track historyKey="/status/sessions/history/4" ratingKey="900" key="/library/metadata/900"
         title="Some Song" type="track" viewedAt="2700" accountID="1"/>
  <Video historyKey="/status/sessions/history/3" title="Ghost entry (deleted)" type="episode"
         viewedAt="2500" accountID="1"/>
</MediaContainer>"""

EPISODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
  <Video ratingKey="101" type="episode" title="Pilot" grandparentTitle="Some Show"
         parentIndex="1" index="3">
    <Guid id="imdb://tt0959621"/>
    <Guid id="tmdb://62085"/>
    <Guid id="tvdb://349232"/>
  </Video>
</MediaContainer>"""


@responses.activate
def test_recent_history_parses_and_skips_keyless_entries():
    responses.get(f"{BASE}/status/sessions/history/all", body=HISTORY_XML)
    entries = PlexClient(BASE, "tok").recent_history()
    assert [e.rating_key for e in entries] == ["101", "202"]
    e = entries[0]
    assert (e.viewed_at, e.account_id, e.title, e.type) == (2000, 1, "Pilot", "episode")
    assert e.dedup_key == "101:2000"
    # token + filters sent
    assert "X-Plex-Token=tok" in responses.calls[0].request.url
    assert "accountID=1" in responses.calls[0].request.url


@responses.activate
def test_metadata_extracts_guids_and_episode_fields():
    responses.get(f"{BASE}/library/metadata/101", body=EPISODE_XML)
    item = PlexClient(BASE, "tok").metadata("101")
    assert item.type == "episode"
    assert item.guids == {"imdb": "tt0959621", "tmdb": "62085", "tvdb": "349232"}
    assert (item.grandparent_title, item.season, item.episode) == ("Some Show", 1, 3)


@responses.activate
def test_metadata_404_raises_not_found():
    responses.get(f"{BASE}/library/metadata/999", status=404)
    with pytest.raises(PlexNotFound):
        PlexClient(BASE, "tok").metadata("999")


@responses.activate
def test_metadata_empty_container_raises_not_found():
    responses.get(
        f"{BASE}/library/metadata/998",
        body='<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>',
    )
    with pytest.raises(PlexNotFound):
        PlexClient(BASE, "tok").metadata("998")


@responses.activate
def test_non_xml_response_raises_plex_error():
    from plex_tvtime_sync.plex_client import PlexError

    responses.get(f"{BASE}/library/metadata/997", body="<html>502 Bad Gateway</html")
    with pytest.raises(PlexError):
        PlexClient(BASE, "tok").metadata("997")
