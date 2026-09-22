"""
JARC's EYE View - Backend
========================
FastAPI + WebSocket. Aviones en vivo (OpenSky) sobre el globo, más:
  #1 estelas/interpolación (lo hace el frontend con los datos que enviamos)
  #2 filtros (frontend)
  #3 alertas de emergencia -> Telegram
  #4 origen/destino por callsign (hexdb.io)
  +  rastreo de un vuelo concreto con avisos a Telegram a 30/20/15/10/5 min y aterrizaje.

Mensajes WebSocket
------------------
Entrada (frontend -> backend):
  {"type":"bbox", lamin,lomin,lamax,lomax}   zona visible a consultar
  {"type":"route", "callsign":"..."}          pedir origen/destino de un vuelo
  {"type":"track", "id":"icao24", "callsign":"..."}   empezar a rastrear
  {"type":"untrack"}                          dejar de rastrear
Salida (backend -> frontend):
  {"type":"state", entities:[...], error:""}
  {"type":"route", callsign, origin, dest}
  {"type":"track", ...estado del rastreo...}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import html
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# Permite `from services import ...` sin importar desde dónde se lance uvicorn.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services import (OpenSky, RouteService, Telegram, aclose, alpr_cameras,
                      analyze_scene, earthquakes, enhance_image, flight_status,
                      geoip, gmap_tile, haversine_km, iss_position, radio_stations,
                      rain_radar, revgeo, tomtom_tile, traffic_incidents,
                      voice_intent, webcams)

load_dotenv()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "8"))
DEFAULT_BBOX = (18.5, -100.5, 20.5, -98.0)
THRESHOLDS = [30, 20, 15, 10, 5]  # minutos antes de aterrizar

opensky = OpenSky()
routes = RouteService()
telegram = Telegram()


# --------------------------------------------------------------------------
# Estado del rastreo de UN vuelo específico
# --------------------------------------------------------------------------
@dataclass
class Track:
    icao24: str
    callsign: str
    resolved: bool = False           # ¿ya buscamos la ruta?
    origin: dict | None = None
    dest: dict | None = None
    fired: set = field(default_factory=set)   # umbrales ya notificados
    inited: bool = False             # ¿ya fijamos umbrales pasados al iniciar?
    lost: int = 0                    # ciclos sin señal
    done: bool = False               # aterrizó / terminado
    # última posición conocida (para dibujar en Google Earth aunque esté fuera de la vista)
    lat: float | None = None
    lon: float | None = None
    alt: float = 0
    hdg: float = 0
    eta: float | None = None
    dist: float | None = None
    fs: dict | None = None   # estado de vuelo (terminal, gate, horarios, delay)


class World:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.bbox: tuple = DEFAULT_BBOX
        self.snapshot: list[dict] = []
        self.last_error: str = ""
        self.last_kml: float = 0.0
        self.track: Track | None = None
        self.emergencies: dict[str, str] = {}   # icao24 -> squawk ya alertado


world = World()


class AIS:
    """Gestor del stream de barcos (AISStream) suscrito al bbox visible."""

    def __init__(self) -> None:
        self.enabled = False
        self.task: asyncio.Task | None = None
        self.ships: dict = {}
        self.sub_bbox: tuple | None = None

    def start(self) -> None:
        self.enabled = True
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self.enabled = False
        self.ships = {}

    def snapshot(self) -> list:
        return list(self.ships.values())

    @staticmethod
    def _sub(key: str, bbox: tuple) -> dict:
        lamin, lomin, lamax, lomax = bbox
        return {"APIKey": key, "BoundingBoxes": [[[lamin, lomin], [lamax, lomax]]],
                "FilterMessageTypes": ["PositionReport"]}

    def _ingest(self, raw: str) -> None:
        try:
            m = json.loads(raw)
        except Exception:
            return
        md = m.get("MetaData") or {}
        rep = (m.get("Message") or {}).get("PositionReport") or {}
        mmsi = md.get("MMSI")
        lat = rep.get("Latitude", md.get("latitude"))
        lon = rep.get("Longitude", md.get("longitude"))
        if mmsi is None or lat is None or lon is None:
            return
        self.ships[mmsi] = {
            "id": str(mmsi), "name": (md.get("ShipName", "") or "").strip() or str(mmsi),
            "lat": lat, "lon": lon, "cog": rep.get("Cog"), "sog": rep.get("Sog"),
            "heading": rep.get("TrueHeading"), "t": time.time(),
        }

    def _prune(self) -> None:
        cut = time.time() - 180
        for k in [k for k, v in self.ships.items() if v["t"] < cut]:
            del self.ships[k]

    async def _run(self) -> None:
        key = os.getenv("AISSTREAM_KEY", "")
        if not key:
            return
        try:
            from websockets.asyncio.client import connect
        except Exception:
            from websockets.client import connect
        while self.enabled:
            try:
                async with connect("wss://stream.aisstream.io/v0/stream", open_timeout=15) as ws:
                    self.sub_bbox = world.bbox
                    await ws.send(json.dumps(self._sub(key, world.bbox)))
                    while self.enabled:
                        if world.bbox != self.sub_bbox:   # el usuario movió el mapa
                            self.sub_bbox = world.bbox
                            await ws.send(json.dumps(self._sub(key, world.bbox)))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        self._ingest(raw)
                        self._prune()
            except Exception:
                if self.enabled:
                    await asyncio.sleep(3)


ais = AIS()


async def broadcast(obj: dict) -> None:
    payload = json.dumps(obj)
    for ws in list(world.clients):
        try:
            await ws.send_text(payload)
        except Exception:
            world.clients.discard(ws)


# --------------------------------------------------------------------------
# #3 Alertas de emergencia
# --------------------------------------------------------------------------
async def check_emergencies(aircraft: list[dict]) -> None:
    current = {}
    for a in aircraft:
        if a["status"] == "alert":
            current[a["id"]] = a["squawk"]
            if world.emergencies.get(a["id"]) != a["squawk"]:
                meaning = {"7500": "secuestro", "7600": "fallo de radio",
                           "7700": "emergencia general"}.get(a["squawk"], "emergencia")
                await telegram.send(
                    f"🚨 <b>EMERGENCIA</b> — squawk {a['squawk']} ({meaning})\n"
                    f"Vuelo <b>{a['name']}</b> ({a['country']})\n"
                    f"Pos: {a['lat']:.3f}, {a['lon']:.3f} · alt {round(a['alt'])} m"
                )
    world.emergencies = current  # olvida los que ya no están en emergencia


# --------------------------------------------------------------------------
# Poller: aviones visibles en el bbox
# --------------------------------------------------------------------------
async def poller() -> None:
    while True:
        active = bool(world.clients) or (time.time() - world.last_kml < 30)
        if active:   # solo consultar OpenSky si hay alguien mirando (ahorra cuota)
            try:
                aircraft = [asdict(a) for a in await opensky.fetch(bbox=world.bbox)]
                world.snapshot = aircraft
                world.last_error = ""
                await check_emergencies(aircraft)
            except httpx.HTTPStatusError as e:
                world.last_error = f"OpenSky HTTP {e.response.status_code}" + (
                    " (cuota agotada)" if e.response.status_code == 429 else "")
            except Exception as e:
                world.last_error = f"{type(e).__name__}: {e}"
            await broadcast({"type": "state", "entities": world.snapshot, "error": world.last_error})
            if ais.enabled:
                await broadcast({"type": "ships", "vessels": ais.snapshot()})
        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# Rastreador: sigue UN vuelo, calcula ETA y notifica por Telegram
# --------------------------------------------------------------------------
def _eta_minutes(a: dict, dest: dict | None) -> tuple[float | None, float | None]:
    """Devuelve (distancia_km, eta_min) al destino, o (None, None)."""
    if not dest:
        return None, None
    dist = haversine_km(a["lat"], a["lon"], dest["lat"], dest["lon"])
    kmh = a["speed"] * 3.6
    if kmh < 40:  # casi parado / en tierra: ETA no fiable
        return dist, None
    return dist, (dist / kmh) * 60.0


def _fmt_time(iso: str | None) -> str | None:
    """'2026-09-21T08:10:00+00:00' -> '08:10' (hora local del aeropuerto)."""
    return iso[11:16] if iso and len(iso) >= 16 else None


def _fs_summary(fs: dict | None) -> str:
    """Línea con terminal/gate/hora/delay para Telegram."""
    if not fs:
        return ""
    parts = []
    if fs.get("arr_terminal"):
        parts.append(f"Terminal {fs['arr_terminal']}")
    if fs.get("arr_gate"):
        parts.append(f"Gate {fs['arr_gate']}")
    sched, est = _fmt_time(fs.get("arr_scheduled")), _fmt_time(fs.get("arr_estimated"))
    if sched:
        parts.append(f"Llegada prog. {sched}")
    if est and est != sched:
        parts.append(f"est. {est}")
    if fs.get("arr_delay"):
        parts.append(f"⏰ {fs['arr_delay']} min retraso")
    return ("\n" + " · ".join(parts)) if parts else ""


async def _notify_track(t: Track, text: str) -> None:
    await telegram.send(text)


async def tracker() -> None:
    while True:
        t = world.track
        if not t or t.done:
            await asyncio.sleep(2)
            continue

        # Resolver ruta una sola vez
        if not t.resolved:
            r = await routes.route(t.callsign)
            if r:
                t.origin, t.dest = r.get("origin"), r.get("dest")
            t.fs = await flight_status(t.callsign)
            t.resolved = True
            org = t.origin["name"] if t.origin else "¿?"
            dst = t.dest["name"] if t.dest else "¿?"
            msg = f"🛰️ Rastreando <b>{t.callsign}</b>\nRuta: {org} → {dst}"
            if not t.dest:
                msg += "\n⚠️ Destino desconocido: avisaré solo del aterrizaje."
            msg += _fs_summary(t.fs)
            await telegram.send(msg)

        # Posición actual del avión rastreado
        try:
            found = await opensky.fetch(icao24=t.icao24)
        except Exception:
            found = []

        if not found:
            t.lost += 1
            # Si lo perdimos cerca del final, asumimos aterrizaje.
            if t.lost >= 3 and t.inited and "landed" not in t.fired:
                t.fired.add("landed")
                t.done = True
                await telegram.send(f"🛬 <b>{t.callsign}</b> desapareció del radar cerca del destino. Probable aterrizaje.")
            await broadcast({"type": "track", "callsign": t.callsign, "phase": "lost",
                             "message": "sin señal", "fired": sorted(t.fired)})
            await asyncio.sleep(POLL_INTERVAL)
            continue

        t.lost = 0
        a = asdict(found[0])
        dist, eta = _eta_minutes(a, t.dest)
        # recordar posición para poder dibujarla en Google Earth
        t.lat, t.lon, t.alt, t.hdg = a["lat"], a["lon"], a["alt"], a["heading"]
        t.eta, t.dist = eta, dist

        # Al iniciar: marcar como "ya pasados" los umbrales por encima del ETA actual
        if not t.inited:
            t.inited = True
            if eta is not None:
                t.fired |= {str(th) for th in THRESHOLDS if eta <= th}

        # Aterrizaje: en tierra, o muy cerca del destino
        landed = a["on_ground"] or (dist is not None and dist < 3)
        if landed and "landed" not in t.fired:
            t.fired.add("landed")
            t.done = True
            t.fs = await flight_status(t.callsign) or t.fs  # refresca gate/hora final
            await telegram.send(f"🛬 <b>{t.callsign}</b> ha aterrizado" +
                                (f" en {t.dest['name']}." if t.dest else ".") + _fs_summary(t.fs))

        # Umbrales de tiempo restante
        if eta is not None:
            for th in THRESHOLDS:
                if eta <= th and str(th) not in t.fired:
                    t.fired.add(str(th))
                    await telegram.send(
                        f"⏱️ <b>{t.callsign}</b>: ~{th} min para aterrizar"
                        + (f" en {t.dest['name']}" if t.dest else "")
                        + f"\nDistancia {dist:.0f} km · alt {round(a['alt'])} m · {a['speed']*3.6:.0f} km/h"
                    )

        await broadcast({
            "type": "track", "callsign": t.callsign, "icao24": t.icao24,
            "phase": "landed" if t.done else "tracking",
            "aircraft": a,
            "origin": t.origin, "dest": t.dest,
            "dist_km": dist, "eta_min": eta,
            "fired": sorted(t.fired), "fs": t.fs,
        })
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(poller()), asyncio.create_task(tracker())]
    yield
    ais.stop()
    if ais.task:
        ais.task.cancel()
    for t in tasks:
        t.cancel()
    await aclose()


app = FastAPI(title="JARC's EYE View", lifespan=lifespan)


@app.get("/api/geoip")
async def api_geoip() -> JSONResponse:
    return JSONResponse(await geoip() or {})


@app.get("/api/radio")
async def api_radio(lat: float, lon: float, limit: int = 30) -> JSONResponse:
    return JSONResponse({"stations": await radio_stations(lat, lon, limit)})


@app.get("/api/webcams")
async def api_webcams(lat: float, lon: float, limit: int = 25,
                      category: str = "") -> JSONResponse:
    return JSONResponse({"webcams": await webcams(lat, lon, limit, category=category)})


@app.get("/api/quakes")
async def api_quakes() -> JSONResponse:
    return JSONResponse({"quakes": await earthquakes()})


@app.get("/api/iss")
async def api_iss() -> JSONResponse:
    return JSONResponse(await iss_position() or {})


@app.get("/api/rain")
async def api_rain() -> JSONResponse:
    return JSONResponse(await rain_radar())


@app.get("/api/alpr")
async def api_alpr(s: float, w: float, n: float, e: float) -> JSONResponse:
    return JSONResponse({"cameras": await alpr_cameras(s, w, n, e)})


@app.get("/api/incidents")
async def api_incidents(s: float, w: float, n: float, e: float) -> JSONResponse:
    return JSONResponse({"incidents": await traffic_incidents(s, w, n, e)})


@app.post("/api/voice")
async def api_voice(payload: dict) -> JSONResponse:
    return JSONResponse(await voice_intent(payload.get("text", "")) or {"action": "say", "text": "No te entendí."})


@app.get("/api/revgeo")
async def api_revgeo(lat: float, lon: float) -> JSONResponse:
    return JSONResponse(await revgeo(lat, lon) or {})


@app.post("/api/analyze")
async def api_analyze(payload: dict) -> JSONResponse:
    d = await analyze_scene(payload.get("image", ""), payload.get("lat", 0.0), payload.get("lon", 0.0))
    return JSONResponse(d or {"error": "sin análisis"})


@app.post("/api/enhance")
async def api_enhance(payload: dict) -> JSONResponse:
    b64 = await enhance_image(payload.get("image", ""), payload.get("prompt", ""))
    return JSONResponse({"b64": b64} if b64 else {"error": "sin imagen"})


_TILE_HEADERS = {"Cache-Control": "public, max-age=60"}


@app.get("/tiles/traffic/{z}/{x}/{y}.png")
async def tile_traffic(z: int, x: int, y: int) -> Response:
    return Response(await tomtom_tile("traffic", z, x, y) or b"", media_type="image/png", headers=_TILE_HEADERS)


@app.get("/tiles/labels/{z}/{x}/{y}.png")
async def tile_labels(z: int, x: int, y: int) -> Response:
    return Response(await tomtom_tile("labels", z, x, y) or b"", media_type="image/png", headers=_TILE_HEADERS)


@app.get("/tiles/gmap/{z}/{x}/{y}.png")
async def tile_gmap(z: int, x: int, y: int) -> Response:
    return Response(await gmap_tile(z, x, y, "roadmap") or b"", media_type="image/png", headers=_TILE_HEADERS)


@app.get("/tiles/gsat/{z}/{x}/{y}.png")
async def tile_gsat(z: int, x: int, y: int) -> Response:
    return Response(await gmap_tile(z, x, y, "satellite") or b"", media_type="image/png", headers=_TILE_HEADERS)


@app.get("/config")
async def config() -> JSONResponse:
    return JSONResponse({
        "googleApiKey": os.getenv("GOOGLE_MAPS_API_KEY", ""),
        "cesiumIonToken": os.getenv("CESIUM_ION_TOKEN", ""),
        "openskyAuth": opensky.has_credentials,
        "telegram": telegram.configured,
        "flightStatus": bool(os.getenv("AVIATIONSTACK_KEY", "")),
        "webcams": bool(os.getenv("WINDY_WEBCAMS_KEY", "")),
        "voice": bool(os.getenv("OPENAI_API_KEY", "")),
        "ai": bool(os.getenv("OPENAI_API_KEY", "")),
        "traffic": bool(os.getenv("TOMTOM_KEY", "")),
        "gmap2d": bool(os.getenv("GOOGLE_MAPS_API_KEY", "")),
        "ais": bool(os.getenv("AISSTREAM_KEY", "")),
        "pollInterval": POLL_INTERVAL,
        "thresholds": THRESHOLDS,
    })


# ==========================================================================
# Rastreo por callsign (usado por el WebSocket y por el panel de Google Earth)
# ==========================================================================
def start_track(callsign: str, icao24: str | None = None) -> tuple[bool, str]:
    cs = (callsign or "").strip().upper()
    if not cs and not icao24:
        return False, "Falta el callsign."
    if not icao24:
        for a in world.snapshot:  # buscar el icao24 en el área visible actual
            if a["name"].strip().upper() == cs:
                icao24 = a["id"]
                break
    if not icao24:
        return False, f"No encontré {cs} en el área visible. Acerca la vista al avión y reintenta."
    world.track = Track(icao24=icao24.lower(), callsign=cs or icao24)
    return True, f"Rastreando {cs or icao24}."


# ==========================================================================
# Google Earth: KML dinámico
# ==========================================================================
KML_MIME = "application/vnd.google-earth.kml+xml"
ARROW = "http://maps.google.com/mapfiles/kml/shapes/arrow.png"
AIRPORT = "http://maps.google.com/mapfiles/kml/shapes/airports.png"
KML_COLOR = {"ok": "ff8ae05f", "warn": "ff55ccff", "alert": "ff0000ff"}  # aabbggrr
TRACK_COLOR = "ffd84fff"
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _placemark(a: dict, cached_route: dict | None) -> str:
    color = KML_COLOR.get(a["status"], "ffffffff")
    kmh = a["speed"] * 3.6
    route_html = ""
    if cached_route and (cached_route.get("origin") or cached_route.get("dest")):
        o = (cached_route.get("origin") or {}).get("icao", "¿?")
        d = (cached_route.get("dest") or {}).get("icao", "¿?")
        route_html = f"<tr><td>Ruta</td><td>{o} → {d}</td></tr>"
    desc = (
        "<table>"
        f"<tr><td>ICAO24</td><td>{a['id']}</td></tr>"
        f"<tr><td>País</td><td>{html.escape(a['country'])}</td></tr>"
        f"<tr><td>Altitud</td><td>{round(a['alt'])} m</td></tr>"
        f"<tr><td>Velocidad</td><td>{kmh:.0f} km/h</td></tr>"
        f"<tr><td>Rumbo</td><td>{round(a['heading'])}°</td></tr>"
        f"<tr><td>Squawk</td><td>{a['squawk'] or '—'}</td></tr>"
        f"{route_html}</table>"
    )
    return (
        "<Placemark>"
        f"<name>{html.escape(a['name'])}</name>"
        f"<description><![CDATA[{desc}]]></description>"
        "<Style><IconStyle>"
        f"<color>{color}</color><scale>0.9</scale>"
        f"<heading>{a['heading']:.0f}</heading>"
        f"<Icon><href>{ARROW}</href></Icon>"
        "</IconStyle><LabelStyle><scale>0.7</scale></LabelStyle></Style>"
        "<Point><altitudeMode>absolute</altitudeMode>"
        f"<coordinates>{a['lon']:.5f},{a['lat']:.5f},{max(a['alt'],0):.0f}</coordinates></Point>"
        "</Placemark>"
    )


def _track_kml() -> str:
    t = world.track
    if not t or t.lat is None:
        return ""
    parts = [
        "<Placemark><name>🎯 " + html.escape(t.callsign) + "</name>",
        f"<description><![CDATA[ETA {('%.0f min' % t.eta) if t.eta is not None else '—'} · "
        f"{('%.0f km' % t.dist) if t.dist is not None else '—'}]]></description>",
        "<Style><IconStyle>"
        f"<color>{TRACK_COLOR}</color><scale>1.4</scale><heading>{t.hdg:.0f}</heading>"
        f"<Icon><href>{ARROW}</href></Icon></IconStyle>"
        "<LabelStyle><scale>1.0</scale></LabelStyle></Style>"
        "<Point><altitudeMode>absolute</altitudeMode>"
        f"<coordinates>{t.lon:.5f},{t.lat:.5f},{max(t.alt,0):.0f}</coordinates></Point></Placemark>",
    ]
    if t.dest:
        parts.append(
            "<Placemark><name>🛬 " + html.escape(t.dest["name"]) + "</name>"
            f"<Style><IconStyle><color>{TRACK_COLOR}</color>"
            f"<Icon><href>{AIRPORT}</href></Icon></IconStyle></Style>"
            "<Point><altitudeMode>clampToGround</altitudeMode>"
            f"<coordinates>{t.dest['lon']:.5f},{t.dest['lat']:.5f},0</coordinates></Point></Placemark>"
        )
        parts.append(
            "<Placemark><Style><LineStyle>"
            f"<color>{TRACK_COLOR}</color><width>2</width></LineStyle></Style>"
            "<LineString><altitudeMode>absolute</altitudeMode><tessellate>1</tessellate>"
            f"<coordinates>{t.lon:.5f},{t.lat:.5f},{max(t.alt,0):.0f} "
            f"{t.dest['lon']:.5f},{t.dest['lat']:.5f},0</coordinates></LineString></Placemark>"
        )
    return f"<Folder><name>Rastreo</name>{''.join(parts)}</Folder>"


@app.get("/earth.kml")
async def earth_kml(request: Request) -> Response:
    """Archivo maestro: ábrelo UNA vez en Google Earth. Trae el NetworkLink en vivo."""
    base = str(request.base_url).rstrip("/")
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <name>JARC's EYE View — Vuelos</name>
  <NetworkLink>
    <name>Vuelos en vivo</name>
    <open>1</open>
    <Link>
      <href>{base}/flights.kml</href>
      <refreshMode>onInterval</refreshMode>
      <refreshInterval>{int(POLL_INTERVAL)}</refreshInterval>
      <viewRefreshMode>onStop</viewRefreshMode>
      <viewRefreshTime>1</viewRefreshTime>
      <viewFormat>BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]</viewFormat>
    </Link>
  </NetworkLink>
</Document></kml>"""
    return Response(kml, media_type=KML_MIME, headers={
        **NO_CACHE, "Content-Disposition": 'attachment; filename="JarcsEyeView.kml"'})


@app.get("/flights.kml")
async def flights_kml(request: Request) -> Response:
    """Devuelto en cada refresco de Google Earth, con los aviones del área visible."""
    world.last_kml = time.time()   # mantiene vivo el poller en modo Google Earth
    bbox = request.query_params.get("BBOX")
    if bbox:
        try:
            w, s, e, n = (float(x) for x in bbox.split(","))
            if -90 <= s < n <= 90 and -180 <= w < e <= 180:
                world.bbox = (s, w, n, e)  # OpenSky: lamin,lomin,lamax,lomax
        except (ValueError, TypeError):
            pass
    marks = "".join(_placemark(a, routes._cache.get(a["name"].strip().upper()))
                    for a in world.snapshot)
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<name>Vuelos</name>" + _track_kml() +
        f"<Folder><name>Aviones ({len(world.snapshot)})</name>{marks}</Folder>"
        "</Document></kml>"
    )
    return Response(kml, media_type=KML_MIME, headers=NO_CACHE)


# ==========================================================================
# Panel de control (para Google Earth): rastrear vuelos, ver estado
# ==========================================================================
@app.post("/api/track")
async def api_track(payload: dict) -> JSONResponse:
    ok, msg = start_track(payload.get("callsign", ""), payload.get("icao24"))
    return JSONResponse({"ok": ok, "message": msg})


@app.post("/api/untrack")
async def api_untrack() -> JSONResponse:
    world.track = None
    return JSONResponse({"ok": True})


@app.get("/api/status")
async def api_status() -> JSONResponse:
    t = world.track
    tinfo = None
    if t:
        tinfo = {
            "callsign": t.callsign, "phase": "landed" if t.done else ("lost" if t.lost else "tracking"),
            "origin": (t.origin or {}).get("name"), "dest": (t.dest or {}).get("name"),
            "eta_min": t.eta, "dist_km": t.dist, "fired": sorted(t.fired),
        }
    return JSONResponse({
        "aircraft": len(world.snapshot), "error": world.last_error,
        "telegram": telegram.configured, "openskyAuth": opensky.has_credentials,
        "track": tinfo,
    })


@app.get("/panel", response_class=HTMLResponse)
async def panel() -> str:
    return PANEL_HTML


def _valid_bbox(m: dict) -> tuple | None:
    try:
        lamin, lomin = float(m["lamin"]), float(m["lomin"])
        lamax, lomax = float(m["lamax"]), float(m["lomax"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lamin < lamax <= 90 and -180 <= lomin < lomax <= 180):
        return None
    return (lamin, lomin, lamax, lomax)


async def _handle_route_request(ws: WebSocket, callsign: str) -> None:
    r = await routes.route(callsign)
    await ws.send_text(json.dumps({
        "type": "route", "callsign": callsign,
        "origin": (r or {}).get("origin"), "dest": (r or {}).get("dest"),
    }))


async def _handle_status_request(ws: WebSocket, callsign: str) -> None:
    fs = await flight_status(callsign)
    await ws.send_text(json.dumps({"type": "status", "callsign": callsign, "fs": fs}))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    world.clients.add(ws)
    await ws.send_text(json.dumps({"type": "state", "entities": world.snapshot, "error": world.last_error}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "bbox":
                bbox = _valid_bbox(msg)
                if bbox:
                    world.bbox = bbox
            elif kind == "route":
                asyncio.create_task(_handle_route_request(ws, msg.get("callsign", "")))
            elif kind == "status":
                asyncio.create_task(_handle_status_request(ws, msg.get("callsign", "")))
            elif kind == "track":
                start_track(msg.get("callsign", ""), msg.get("id"))
            elif kind == "untrack":
                world.track = None
                await broadcast({"type": "track", "phase": "off"})
            elif kind == "ships":
                ais.start() if msg.get("on") else ais.stop()
    except WebSocketDisconnect:
        world.clients.discard(ws)


PANEL_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARC's EYE View — Control (Google Earth)</title>
<style>
  body{font-family:"Segoe UI",system-ui,sans-serif;background:#0a141e;color:#dff;margin:0;padding:24px;}
  .wrap{max-width:640px;margin:0 auto;}
  h1{color:#7fd3ff;font-size:20px;letter-spacing:1px;}
  .card{background:#0e1c2a;border:1px solid #1f3d55;border-radius:12px;padding:18px 20px;margin:16px 0;}
  a.btn,button{display:inline-block;background:#134;color:#cfefff;border:1px solid #2a6a8f;
    border-radius:8px;padding:10px 16px;font-size:14px;cursor:pointer;text-decoration:none;}
  button.stop{background:#5a1420;border-color:#8a2a3a;color:#ffdada;}
  input{background:#0a141e;color:#dff;border:1px solid #2a4a63;border-radius:8px;padding:9px 12px;font-size:14px;width:180px;}
  ol{line-height:1.7;} code{color:#7fd3ff;background:#08121c;padding:1px 6px;border-radius:4px;}
  .kv{display:flex;justify-content:space-between;font-size:14px;margin:5px 0;border-bottom:1px solid #12283a;padding-bottom:4px;}
  .th{display:inline-block;width:15%;text-align:center;font-size:12px;padding:5px 0;margin:3px 0.3%;
    border-radius:6px;background:#08121c;color:#6b8;}
  .th.done{background:#1f6b3a;color:#bff;} .muted{color:#7a97ad;font-size:13px;} .dot{font-size:12px;}
</style></head><body><div class="wrap">
<h1>🛰️ JARC'S EYE VIEW — CONTROL</h1>

<div class="card">
  <b>1) Abre el globo en Google Earth</b>
  <p class="muted">Descarga y abre este archivo (una sola vez). Google Earth Pro empezará
  a mostrar los aviones del área que veas y se actualizará solo.</p>
  <a class="btn" href="/earth.kml">⬇ Abrir JarcsEyeView.kml en Google Earth</a>
  <ol class="muted">
    <li>Se descarga <code>JarcsEyeView.kml</code> → ábrelo (doble clic) con Google Earth Pro.</li>
    <li>Aparece en <b>«Lugares temporales»</b>. Muévete/haz zoom: los aviones aparecen en la vista.</li>
    <li>Deja este servidor corriendo mientras lo usas.</li>
  </ol>
</div>

<div class="card">
  <b>2) Rastrear un vuelo (avisos a Telegram)</b>
  <p class="muted">Escribe el <b>callsign</b> (p. ej. <code>IBE6250</code>) de un avión que esté
  a la vista en Google Earth. Recibirás avisos a 30/20/15/10/5 min del aterrizaje y al aterrizar.</p>
  <input id="cs" placeholder="Callsign (ej. IBE6250)" />
  <button onclick="track()">🎯 Rastrear</button>
  <button class="stop" onclick="untrack()">✖ Detener</button>
  <div id="msg" class="muted" style="margin-top:10px;"></div>
</div>

<div class="card">
  <b>Estado</b>
  <div id="status" class="muted">cargando…</div>
  <div id="track"></div>
</div>

<script>
const $=(id)=>document.getElementById(id);
async function track(){
  const cs=$("cs").value.trim(); if(!cs) return;
  const r=await (await fetch("/api/track",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({callsign:cs})})).json();
  $("msg").textContent=r.message; $("msg").style.color=r.ok?"#5fe08a":"#ff9aa2";
}
async function untrack(){ await fetch("/api/untrack",{method:"POST"}); $("msg").textContent="Rastreo detenido."; }
async function poll(){
  try{
    const s=await (await fetch("/api/status")).json();
    const tg=s.telegram?"TG✓":"TG✗", au=s.openskyAuth?"OpenSky auth":"OpenSky anónimo";
    $("status").innerHTML=`<div class="kv"><span>Aviones a la vista</span><span>${s.aircraft}</span></div>`
      +`<div class="kv"><span>Fuentes</span><span>${au} · ${tg}</span></div>`
      +(s.error?`<div class="kv"><span>Aviso</span><span style="color:#ff9aa2">${s.error}</span></div>`:"");
    if(s.track){
      const t=s.track, eta=t.eta_min!=null?t.eta_min.toFixed(0)+" min":"—",
        dist=t.dist_km!=null?t.dist_km.toFixed(0)+" km":"—",
        fired=new Set(t.fired||[]);
      const chips=["30","20","15","10","5","landed"].map(k=>
        `<span class="th ${fired.has(k)?"done":""}">${k==="landed"?"🛬":k+"m"}</span>`).join("");
      $("track").innerHTML=`<hr style="border-color:#1f3d55">
        <div class="kv"><span>Rastreando</span><span>${t.callsign}</span></div>
        <div class="kv"><span>Ruta</span><span>${t.origin||"¿?"} → ${t.dest||"¿?"}</span></div>
        <div class="kv"><span>Distancia / ETA</span><span>${dist} · ${eta}</span></div>
        <div class="kv"><span>Fase</span><span>${t.phase==="landed"?"🛬 aterrizó":t.phase==="lost"?"sin señal":"en vuelo"}</span></div>
        <div style="margin-top:8px">${chips}</div>`;
    } else $("track").innerHTML="";
  }catch(e){ $("status").textContent="sin conexión con el servidor"; }
}
poll(); setInterval(poll,3000);
</script></div></body></html>"""


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
