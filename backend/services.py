"""
Servicios externos y utilidades para JARC's EYE View.

- OpenSky:  posiciones ADS-B en vivo (por bbox o por icao24).
- Routes:   origen/destino de un vuelo a partir del callsign (hexdb.io).
- Telegram: envío de notificaciones.
- geo:      distancia haversine.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, asdict

import httpx

# Cliente HTTP compartido (async).
_http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "JarcsEyeView/1.0"})


async def aclose() -> None:
    await _http.aclose()


async def geoip() -> dict | None:
    """Ubicación aproximada por IP pública (el backend corre en la máquina del usuario)."""
    try:
        r = await _http.get("http://ip-api.com/json/")
        d = r.json()
        if d.get("status") == "success":
            return {"lat": d["lat"], "lon": d["lon"],
                    "city": d.get("city", ""), "country": d.get("country", "")}
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Radio: emisoras de internet cercanas (Radio Browser + Nominatim)
# --------------------------------------------------------------------------
RADIO_SERVERS = [
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
_country_cache: dict[tuple, str | None] = {}


async def reverse_country(lat: float, lon: float) -> str | None:
    key = (round(lat, 1), round(lon, 1))
    if key in _country_cache:
        return _country_cache[key]
    cc = None
    try:
        r = await _http.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "json", "lat": lat, "lon": lon, "zoom": 3},
            headers={"User-Agent": "JarcsEyeView/1.0 (jarcmaster@gmail.com)"},
        )
        cc = ((r.json().get("address") or {}).get("country_code") or "").upper() or None
    except Exception:
        cc = None
    _country_cache[key] = cc
    return cc


async def radio_stations(lat: float, lon: float, limit: int = 30) -> list[dict]:
    """Emisoras cercanas: país por reverse-geocode, orden por distancia real."""
    cc = await reverse_country(lat, lon)
    params = {"hidebroken": "true", "order": "votes", "reverse": "true", "limit": "400"}
    if cc:
        params["countrycode"] = cc
        params["has_geo_info"] = "true"
    raw: list = []
    for base in RADIO_SERVERS:
        try:
            r = await _http.get(f"{base}/json/stations/search", params=params)
            if r.status_code == 200:
                raw = r.json()
                break
        except Exception:
            continue
    out = []
    for s in raw:
        url = s.get("url_resolved") or s.get("url")
        if not url:
            continue
        try:
            glat, glon = float(s["geo_lat"]), float(s["geo_long"])
        except (TypeError, ValueError, KeyError):
            glat = glon = None
        out.append({
            "name": (s.get("name") or "").strip() or "(sin nombre)",
            "url": url, "lat": glat, "lon": glon,
            "favicon": s.get("favicon", ""), "country": s.get("country", ""),
            "codec": s.get("codec", ""), "bitrate": s.get("bitrate", 0),
            "votes": s.get("votes", 0), "tags": s.get("tags", ""),
        })
    geo = [s for s in out if s["lat"] is not None]
    geo.sort(key=lambda s: haversine_km(lat, lon, s["lat"], s["lon"]))
    return (geo or out)[:limit]


# --------------------------------------------------------------------------
# Estado de vuelo (terminal, gate, hora de aterrizaje, delays) -> AviationStack
# --------------------------------------------------------------------------
_status_cache: dict[str, tuple[float, dict | None]] = {}


def _pick_flight(data: list) -> dict:
    """Prefiere el vuelo activo, luego aterrizado, si no el primero."""
    for want in ("active", "landed"):
        for f in data:
            if f.get("flight_status") == want:
                return f
    return data[0]


async def flight_status(callsign: str) -> dict | None:
    """Terminal, gate, horarios y delays por callsign (ICAO). Cache 10 min (cuota baja)."""
    key = os.getenv("AVIATIONSTACK_KEY", "")
    cs = (callsign or "").strip().upper()
    if not key or not cs:
        return None
    now = time.monotonic()
    hit = _status_cache.get(cs)
    if hit and now - hit[0] < 600:
        return hit[1]
    result = None
    try:
        r = await _http.get(
            "http://api.aviationstack.com/v1/flights",
            params={"access_key": key, "flight_icao": cs},
        )
        data = (r.json() or {}).get("data") or []
        if data:
            f = _pick_flight(data)
            dep, arr = f.get("departure") or {}, f.get("arrival") or {}
            result = {
                "status": f.get("flight_status"),
                "airline": (f.get("airline") or {}).get("name"),
                "flight": (f.get("flight") or {}).get("iata") or cs,
                "dep_airport": dep.get("iata"), "dep_terminal": dep.get("terminal"),
                "dep_gate": dep.get("gate"), "dep_delay": dep.get("delay"),
                "arr_airport": arr.get("iata"), "arr_terminal": arr.get("terminal"),
                "arr_gate": arr.get("gate"), "arr_baggage": arr.get("baggage"),
                "arr_scheduled": arr.get("scheduled"), "arr_estimated": arr.get("estimated"),
                "arr_actual": arr.get("actual"), "arr_delay": arr.get("delay"),
            }
    except Exception:
        result = None
    _status_cache[cs] = (now, result)
    return result


# --------------------------------------------------------------------------
# Geo
# --------------------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# OpenSky
# --------------------------------------------------------------------------
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}


@dataclass
class Aircraft:
    id: str
    name: str
    kind: str
    lat: float
    lon: float
    alt: float
    heading: float
    speed: float          # m/s
    status: str           # ok | warn (en tierra) | alert (emergencia)
    country: str
    squawk: str
    on_ground: bool


def state_to_aircraft(s: list) -> Aircraft | None:
    lon, lat = s[5], s[6]
    if lat is None or lon is None:
        return None
    on_ground = bool(s[8])
    squawk = s[14] or ""
    status = "alert" if squawk in EMERGENCY_SQUAWKS else ("warn" if on_ground else "ok")
    return Aircraft(
        id=s[0],
        name=(s[1] or "").strip() or s[0],
        kind="aircraft",
        lat=lat, lon=lon,
        alt=(s[13] if s[13] is not None else (s[7] or 0)),
        heading=(s[10] or 0),
        speed=(s[9] or 0),
        status=status,
        country=s[2] or "",
        squawk=squawk,
        on_ground=on_ground,
    )


class OpenSky:
    def __init__(self) -> None:
        self.client_id = os.getenv("OPENSKY_CLIENT_ID", "")
        self.client_secret = os.getenv("OPENSKY_CLIENT_SECRET", "")
        self._token = ""
        self._exp = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _auth(self) -> dict:
        if not self.has_credentials:
            return {}
        if self._token and time.monotonic() < self._exp - 30:
            return {"Authorization": f"Bearer {self._token}"}
        r = await _http.post(OPENSKY_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._exp = time.monotonic() + float(d.get("expires_in", 1800))
        return {"Authorization": f"Bearer {self._token}"}

    async def fetch(self, bbox: tuple | None = None, icao24: str | None = None) -> list[Aircraft]:
        params: dict = {}
        if bbox:
            params.update(lamin=bbox[0], lomin=bbox[1], lamax=bbox[2], lomax=bbox[3])
        if icao24:
            params["icao24"] = icao24.lower()
        r = await _http.get(OPENSKY_STATES_URL, params=params, headers=await self._auth())
        r.raise_for_status()
        states = r.json().get("states") or []
        return [a for a in (state_to_aircraft(s) for s in states) if a]


# --------------------------------------------------------------------------
# Rutas (origen/destino) por callsign  -> hexdb.io (comunitario, gratis)
# --------------------------------------------------------------------------
class RouteService:
    """Origen/destino por callsign usando adsbdb.com (gratis, sin clave)."""

    def __init__(self) -> None:
        self._cache: dict[str, dict | None] = {}

    @staticmethod
    def _airport(d: dict | None) -> dict | None:
        if not d or d.get("latitude") is None:
            return None
        return {
            "icao": d.get("icao_code", ""),
            "iata": d.get("iata_code", ""),
            "name": d.get("name") or d.get("municipality") or d.get("icao_code", ""),
            "lat": float(d["latitude"]),
            "lon": float(d["longitude"]),
            "elev": d.get("elevation"),      # metros sobre el nivel del mar
            "country": d.get("country_name", ""),
        }

    async def route(self, callsign: str) -> dict | None:
        cs = (callsign or "").strip().upper()
        if not cs:
            return None
        if cs in self._cache:
            return self._cache[cs]
        result = None
        try:
            r = await _http.get(f"https://api.adsbdb.com/v0/callsign/{cs}")
            if r.status_code == 200:
                fr = (r.json().get("response") or {}).get("flightroute")
                if isinstance(fr, dict):
                    origin = self._airport(fr.get("origin"))
                    dest = self._airport(fr.get("destination"))
                    airline = (fr.get("airline") or {}).get("name", "")
                    if origin or dest:
                        result = {"origin": origin, "dest": dest, "airline": airline}
        except Exception:
            result = None
        self._cache[cs] = result
        return result


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
class Telegram:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> bool:
        if not self.configured:
            return False
        try:
            r = await _http.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
            )
            return r.status_code == 200
        except Exception:
            return False
