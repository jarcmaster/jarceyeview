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
