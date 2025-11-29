"""Expose Radio Browser as a media source."""

from __future__ import annotations

import mimetypes

import pycountry
from radios import FilterBy, Order, RadioBrowser, Station

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.location import vincenty

from . import RadioBrowserConfigEntry
from .const import DOMAIN

CODEC_TO_MIMETYPE = {
    "MP3": "audio/mpeg",
    "AAC": "audio/aac",
    "AAC+": "audio/aac",
    "OGG": "application/ogg",
}


async def async_get_media_source(hass: HomeAssistant) -> RadioMediaSource:
    """Set up Radio Browser media source."""
    # Radio browser supports only a single config entry
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    return RadioMediaSource(hass, entry)


class RadioMediaSource(MediaSource):
    """Provide Radio stations as media sources."""

    name = "Radio Browser"

    def __init__(self, hass: HomeAssistant, entry: RadioBrowserConfigEntry) -> None:
        """Initialize RadioMediaSource."""
        super().__init__(DOMAIN)
        self.hass = hass
        self.entry = entry

    @property
    def radios(self) -> RadioBrowser:
        """Return the radio browser."""
        return self.entry.runtime_data

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve selected Radio station to a streaming URL."""
        radios = self.radios

        station = await radios.station(uuid=item.identifier)
        if not station:
            raise Unresolvable("Radio station is no longer available")

        if not (mime_type := self._async_get_station_mime_type(station)):
            raise Unresolvable("Could not determine stream type of radio station")

        # Register "click" with Radio Browser
        await radios.station_click(uuid=station.uuid)

        return PlayMedia(station.url_resolved, mime_type)

    async def async_browse_media(
        self,
        item: MediaSourceItem,
    ) -> BrowseMediaSource:
        """Return media."""
        radios = self.radios

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.MUSIC,
            title=self.entry.title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                *await self._async_build_popular(radios, item),
                *await self._async_build_by_tag(radios, item),
                *await self._async_build_by_language(radios, item),
                *await self._async_build_local(radios, item),
                *await self._async_build_by_country(radios, item),
            ],
        )

    @callback
    @staticmethod
    def _async_get_station_mime_type(station: Station) -> str | None:
        """Determine mime type of a radio station."""
        mime_type = CODEC_TO_MIMETYPE.get(station.codec)
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(station.url)
        return mime_type

    @callback
    def _async_build_stations(
        self, radios: RadioBrowser, stations: list[Station]
    ) -> list[BrowseMediaSource]:
        """Build list of media sources from radio stations."""
        items: list[BrowseMediaSource] = []

        for station in stations:
            if station.codec == "UNKNOWN" or not (
                mime_type := self._async_get_station_mime_type(station)
            ):
                continue

            items.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=station.uuid,
                    media_class=MediaClass.MUSIC,
                    media_content_type=mime_type,
                    title=station.name,
                    can_play=True,
                    can_expand=False,
                    thumbnail=station.favicon,
                )
            )

        return items

    async def _async_build_by_country(
        self, radios: RadioBrowser, item: MediaSourceItem
    ) -> list[BrowseMediaSource]:
        """Handle browsing radio stations by country."""
        # Only show Russian stations
        if not item.identifier:
            stations = await radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term='RU',  # Russia country code
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )
            return self._async_build_stations(radios, stations)

        return []

    async def _async_build_by_language(
        self, radios: RadioBrowser, item: MediaSourceItem
    ) -> list[BrowseMediaSource]:
        """Handle browsing radio stations by language."""
        # Only show Russian language stations
        if not item.identifier:
            stations = await radios.stations(
                filter_by=FilterBy.LANGUAGE_EXACT,
                filter_term='russian',  # Russian language
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )
            return self._async_build_stations(radios, stations)

        return []

    async def _async_build_popular(
        self, radios: RadioBrowser, item: MediaSourceItem
    ) -> list[BrowseMediaSource]:
        """Handle browsing popular radio stations."""
        if not item.identifier:
            # Popular Russian stations
            stations = await radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term='RU',  # Russia country code
                hide_broken=True,
                limit=250,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return self._async_build_stations(radios, stations)

        return []

    async def _async_build_by_tag(
        self, radios: RadioBrowser, item: MediaSourceItem
    ) -> list[BrowseMediaSource]:
        """Handle browsing radio stations by tags."""
        category, _, tag = (item.identifier or "").partition("/")
        if category == "tag" and tag:
            # Stations by tag filtered for Russia
            stations = await radios.stations(
                filter_by=FilterBy.TAG_EXACT,
                filter_term=tag,
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )
            # Filter stations to only include Russian ones
            russian_stations = [s for s in stations if s.countrycode == 'RU' or 'russian' in (s.language or '').lower()]
            return self._async_build_stations(radios, russian_stations)

        if category == "tag":
            # Get popular tags from Russian stations
            stations = await radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term='RU',
                hide_broken=True,
            )
            
            # Collect unique tags from Russian stations
            tag_count = {}
            for station in stations:
                if station.tags:
                    for tag in station.tags.split(','):
                        tag = tag.strip()
                        if tag:
                            tag_count[tag] = tag_count.get(tag, 0) + 1
            
            # Convert to list and sort by count
            tags = [{"name": tag, "stationcount": count} for tag, count in tag_count.items()]
            tags.sort(key=lambda x: x["stationcount"], reverse=True)
            tags = tags[:100]  # Limit to top 100 tags
            tags.sort(key=lambda x: x["name"])  # Sort by name

            return [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"tag/{tag['name']}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.MUSIC,
                    title=tag['name'].title(),
                    can_play=False,
                    can_expand=True,
                )
                for tag in tags
            ]

        if not item.identifier:
            return [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier="tag",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.MUSIC,
                    title="By Category",
                    can_play=False,
                    can_expand=True,
                )
            ]

        return []

    def _filter_local_stations(
        self, stations: list[Station], latitude: float, longitude: float
    ) -> list[Station]:
        return [
            station
            for station in stations
            if station.latitude is not None
            and station.longitude is not None
            and station.countrycode == 'RU'  # Only Russian stations
            and (
                (
                    dist := vincenty(
                        (latitude, longitude),
                        (station.latitude, station.longitude),
                        False,
                    )
                )
                is not None
            )
            and dist < 100
        ]

    async def _async_build_local(
        self, radios: RadioBrowser, item: MediaSourceItem
    ) -> list[BrowseMediaSource]:
        """Handle browsing local radio stations."""

        if not item.identifier:
            # Get Russian stations and filter by location
            stations = await radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term='RU',  # Only Russian stations
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )

            local_stations = await self.hass.async_add_executor_job(
                self._filter_local_stations,
                stations,
                self.hass.config.latitude,
                self.hass.config.longitude,
            )

            return self._async_build_stations(radios, local_stations)

        return []
