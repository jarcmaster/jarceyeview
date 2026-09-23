"""
Servicios externos y utilidades para JARC's EYE View.
Author: JOSE RODRIGUEZ, Computer Engineer

- OpenSky:  posiciones ADS-B en vivo (por bbox o por icao24).
- Routes:   origen/destino de un vuelo a partir del callsign (hexdb.io).
- Telegram: envío de notificaciones.
- geo:      distancia haversine.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from dataclasses import dataclass, asdict

import httpx

# Cliente HTTP compartido (async).
_http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "JarcsEyeView/1.0"})


async def aclose() -> None:
    await _http.aclose()


def _openai_ok() -> bool:
    """OpenAI SOLO se usa si hay clave Y no está el modo local (AI_LOCAL_ONLY, activo por defecto).
       Así la IA local (llama.cpp/Ollama) + ComfyUI son el único camino y nunca aparecen
       errores de OpenAI (p.ej. 'no credits')."""
    if not os.getenv("OPENAI_API_KEY", ""):
        return False
    return os.getenv("AI_LOCAL_ONLY", "1").strip().lower() not in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Overpass (OpenStreetMap) — proxied por el backend: sin CORS, con failover y caché.
# El navegador hace muchas consultas y cae en rate-limit/CORS → aquí es más fiable.
# --------------------------------------------------------------------------
_OVERPASS_EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]
_overpass_cache: dict[str, tuple[float, list]] = {}


async def overpass_query(query: str, key: str = "") -> list:
    """Ejecuta una consulta Overpass con failover entre servidores y caché de 5 min por `key`.
       Devuelve la lista de elementos con geometría."""
    if not (query or "").strip():
        return []
    now = time.monotonic()
    if key and key in _overpass_cache:
        t, els = _overpass_cache[key]
        if now - t < 300:
            return els
    for ep in _OVERPASS_EPS:
        try:
            r = await _http.post(ep, data={"data": query}, timeout=25)
            if r.status_code != 200:
                continue
            els = [e for e in (r.json().get("elements") or [])
                   if e.get("geometry") and len(e["geometry"]) > 1]
            if els:
                if key:
                    _overpass_cache[key] = (now, els)
                return els
        except Exception:
            continue
    return []


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
# Webcams en vivo cercanas -> Windy Webcams API v3
# --------------------------------------------------------------------------
async def webcams(lat: float, lon: float, limit: int = 25, radius: int = 100,
                  category: str = "") -> list[dict]:
    key = os.getenv("WINDY_WEBCAMS_KEY", "")
    if not key:
        return []
    params = {"nearby": f"{lat},{lon},{radius}", "limit": limit,
              "include": "images,location,player,urls"}
    if category:
        params["categories"] = category   # p.ej. "traffic" (cámaras de carretera)
    try:
        r = await _http.get(
            "https://api.windy.com/webcams/api/v3/webcams",
            params=params, headers={"x-windy-api-key": key},
        )
        cams = (r.json() or {}).get("webcams") or []
    except Exception:
        return []
    out = []
    for c in cams:
        loc = c.get("location") or {}
        cur = (c.get("images") or {}).get("current") or {}
        player = c.get("player") or {}
        urls = c.get("urls") or {}
        if loc.get("latitude") is None:
            continue
        out.append({
            "title": c.get("title") or "webcam",
            "lat": loc["latitude"], "lon": loc["longitude"],
            "city": loc.get("city", ""), "country": loc.get("country", ""),
            "preview": cur.get("preview") or cur.get("thumbnail") or "",
            "embed": player.get("day") or "",
            "detail": urls.get("detail") or "",
            "views": c.get("viewCount", 0),
            "updated": c.get("lastUpdatedOn"),
        })
    return out


# --------------------------------------------------------------------------
# Capas globales sin key: terremotos (USGS), ISS, radar de lluvia (RainViewer)
# --------------------------------------------------------------------------
async def earthquakes() -> list[dict]:
    """Sismos M2.5+ de las últimas 24 h (USGS)."""
    try:
        r = await _http.get(
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson")
        feats = (r.json() or {}).get("features") or []
    except Exception:
        return []
    out = []
    for f in feats:
        c = (f.get("geometry") or {}).get("coordinates") or []
        p = f.get("properties") or {}
        if len(c) < 2:
            continue
        out.append({"lon": c[0], "lat": c[1], "depth": c[2] if len(c) > 2 else None,
                    "mag": p.get("mag"), "place": p.get("place", ""),
                    "time": p.get("time"), "url": p.get("url", "")})
    return out


async def iss_position() -> dict | None:
    """Posición en vivo de la ISS (wheretheiss.at, sin key)."""
    try:
        d = (await _http.get("https://api.wheretheiss.at/v1/satellites/25544")).json()
        return {"lat": d["latitude"], "lon": d["longitude"], "alt": d.get("altitude"),
                "vel": d.get("velocity")}
    except Exception:
        return None


async def firms_fires(s: float, w: float, n: float, e: float,
                      source: str = "VIIRS_SNPP_NRT", days: int = 1) -> list[dict]:
    """Focos de incendio activos (NASA FIRMS) en el bbox, últimas 24 h."""
    key = os.getenv("FIRMS_MAP_KEY", "")
    if not key:
        return []
    area = f"{w},{s},{e},{n}"  # FIRMS espera west,south,east,north
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/{days}"
    try:
        text = (await _http.get(url)).text
    except Exception:
        return []
    lines = text.strip().splitlines()
    if len(lines) < 2 or "," not in lines[0]:
        return []
    header = [h.strip() for h in lines[0].split(",")]

    def col(name: str) -> int:
        return header.index(name) if name in header else -1

    ilat, ilon = col("latitude"), col("longitude")
    ifrp, iconf = col("frp"), col("confidence")
    idate, itime, idn = col("acq_date"), col("acq_time"), col("daynight")
    ibr = col("bright_ti4") if col("bright_ti4") >= 0 else col("brightness")
    if ilat < 0 or ilon < 0:
        return []
    out: list[dict] = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) <= max(ilat, ilon):
            continue
        try:
            lat, lon = float(p[ilat]), float(p[ilon])
        except Exception:
            continue

        def g(i: int) -> str:
            return p[i].strip() if 0 <= i < len(p) else ""

        try:
            frp = float(g(ifrp)) if ifrp >= 0 and g(ifrp) else None
        except Exception:
            frp = None
        out.append({"lat": lat, "lon": lon, "frp": frp, "conf": g(iconf),
                    "date": g(idate), "time": g(itime), "daynight": g(idn), "bright": g(ibr)})
        if len(out) >= 3000:
            break
    return out


VOICE_SYSTEM = """Eres el copiloto de voz de JARC'S EYE View, un mapa 3D tipo "God's Eye".
Convierte la orden del usuario (español o inglés) en UNA sola acción JSON.
Responde SOLO JSON válido: {"action":"...", ...campos, "text":"confirmación breve en español"}.

Acciones válidas:
- {"action":"flyto","place":"<lugar>","text":"..."}          (volar a una ciudad/lugar)
- {"action":"mylocation","text":"..."}                        (ir a mi ubicación)
- {"action":"layer","layer":"planes|quakes|iss|rain|traffic|streets|ships|alpr|inc|fires|veh","on":true|false,"text":"..."}
- {"action":"pois","kind":"parks","on":true|false,"text":"..."}  (mostrar/ocultar parques y zonas verdes de la vista)
- {"action":"scan","kind":"radio|webcams|traffic","text":"..."}
- {"action":"preset","preset":"normal|crt|nvg|flir|anime|noir|snow","text":"..."}
- {"action":"mapsource","source":"google3d|bing|binglabels|esri|osm","text":"..."}
- {"action":"track","callsign":"<callsign>","text":"..."}     (rastrear un vuelo)
- {"action":"streetview","text":"..."}                        (abrir Street View del punto actual)
- {"action":"generate","prompt":"<descripción en inglés para el generador>","text":"..."}  (generar/crear una imagen)
- {"action":"carfollow","orbit":true|false,"text":"..."}      (seguir el vehículo seleccionado; orbit=false = sin órbita)
- {"action":"cartag","tag":"<nombre>","text":"..."}           (etiquetar el vehículo; omite "tag" para que la app la pida)
- {"action":"cartrackoff","text":"..."}                       (cancelar el rastreo del vehículo)
- {"action":"daynight","mode":"day|night|off","text":"..."}   (vista de día / de noche / apagar)
- {"action":"objetivo","on":true|false,"text":"..."}          (modo OBJETIVO / spy zoom)
- {"action":"north","text":"..."}                             (orientar la cámara al norte)
- {"action":"hover","on":true|false,"text":"..."}             (sobrevolar/orbitar el objetivo)
- {"action":"detect","kind":"people|structures|vehicles","on":true|false,"text":"..."}  (personas/estructuras/vehículos)
- {"action":"optic","mode":"termal|heat|nvg|flir|noir|sar|uv|desert","text":"..."}  (óptica del visor; omite mode para abrir el menú)
- {"action":"photo","text":"..."}                             (tomar foto)
- {"action":"gallery","on":true|false,"text":"..."}           (abrir/cerrar la galería)
- {"action":"panels","text":"..."}                            (mostrar/ocultar el panel de CAPAS)
- {"action":"close","text":"..."}                             (cerrar las ventanas flotantes)
- {"action":"say","text":"<respuesta breve>"}                 (si no es un comando o es una pregunta)

Ejemplos:
"genera una imagen de un dron sobrevolando la ciudad" -> {"action":"generate","prompt":"a surveillance drone flying over a city at dusk, cinematic","text":"Generando imagen"}
"vuela a Tokio" -> {"action":"flyto","place":"Tokyo","text":"Volando a Tokio"}
"muéstrame los terremotos" -> {"action":"layer","layer":"quakes","on":true,"text":"Mostrando terremotos"}
"apaga la lluvia" -> {"action":"layer","layer":"rain","on":false,"text":"Lluvia apagada"}
"visión nocturna" -> {"action":"preset","preset":"nvg","text":"Modo NVG"}
"modo térmico" -> {"action":"preset","preset":"flir","text":"Modo FLIR"}
"cambia a OpenStreetMap" -> {"action":"mapsource","source":"osm","text":"Mapa OSM"}
"escanea radios" -> {"action":"scan","kind":"radio","text":"Escaneando radios"}
"cámaras de carretera" -> {"action":"scan","kind":"traffic","text":"Cámaras de tráfico"}
"muéstrame los parques de esta zona" -> {"action":"pois","kind":"parks","on":true,"text":"Marcando los parques"}
"enciende las cámaras ALPR" -> {"action":"layer","layer":"alpr","on":true,"text":"Cámaras ALPR activadas"}
"sigue este vehículo en órbita" -> {"action":"carfollow","orbit":true,"text":"Siguiendo en órbita"}
"síguelo sin órbita" -> {"action":"carfollow","orbit":false,"text":"Siguiendo sin órbita"}
"ponle la etiqueta sospechoso uno" -> {"action":"cartag","tag":"SOSPECHOSO 1","text":"Etiqueta aplicada"}
"aplica una etiqueta" -> {"action":"cartag","text":"¿Qué etiqueta le pongo?"}
"cancela el rastreo" -> {"action":"cartrackoff","text":"Rastreo cancelado"}
"pon la vista de noche" -> {"action":"daynight","mode":"night","text":"Modo noche"}
"activa el objetivo" -> {"action":"objetivo","on":true,"text":"Objetivo activado"}
"orienta al norte" -> {"action":"north","text":"Al norte"}
"muéstrame las personas" -> {"action":"detect","kind":"people","on":true,"text":"Mostrando personas"}
"óptica térmica" -> {"action":"optic","mode":"termal","text":"Óptica térmica"}
"toma una foto" -> {"action":"photo","text":"Tomando foto"}
"abre la galería" -> {"action":"gallery","on":true,"text":"Galería"}
"cierra las ventanas" -> {"action":"close","text":"Cerrando ventanas"}
"llévame a casa" -> {"action":"mylocation","text":"Yendo a tu ubicación"}"""


# --------------------------------------------------------------------------
# IA LOCAL: Ollama (texto + visión) + Stable Diffusion (AUTOMATIC1111)
# Todo pasa por el backend (mismo origen que el frontend) → sin problemas CORS.
# --------------------------------------------------------------------------
def ollama_host() -> str:
    return (os.getenv("OLLAMA_HOST", "http://localhost:11434") or "").rstrip("/")


def sd_host() -> str:
    return (os.getenv("SD_HOST", "http://localhost:7860") or "").rstrip("/")


# --------------------------------------------------------------------------
# GENERADOR DE IMÁGENES: ComfyUI (API de workflow en /prompt) — soporta FLUX/SD/SDXL/SD3
# --------------------------------------------------------------------------
def comfy_host() -> str:
    return (os.getenv("COMFY_HOST", "http://127.0.0.1:8188") or "").rstrip("/")


_comfy_cache: dict = {"ckpt": None, "t": 0.0, "up": None}


async def comfy_ready() -> bool:
    host = comfy_host()
    if not host:
        return False
    try:
        r = await _http.get(f"{host}/system_stats", timeout=4)
        return r.status_code == 200
    except Exception:
        return False


async def comfy_checkpoint() -> str:
    """Primer checkpoint disponible en ComfyUI (o COMFY_CKPT si se fija). Cacheado ~60 s."""
    env = os.getenv("COMFY_CKPT", "").strip()
    if env:
        return env
    now = time.monotonic()
    if _comfy_cache["ckpt"] and now - _comfy_cache["t"] < 60:
        return _comfy_cache["ckpt"]
    ckpt = ""
    try:
        r = await _http.get(f"{comfy_host()}/object_info/CheckpointLoaderSimple", timeout=8)
        r.raise_for_status()
        opts = r.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        ckpt = opts[0] if opts else ""
    except Exception:
        pass
    _comfy_cache["ckpt"], _comfy_cache["t"] = ckpt, now
    return ckpt


async def _comfy_upload(image: str) -> str | None:
    """Sube una imagen base64 a ComfyUI (para img2img). Devuelve el nombre subido."""
    try:
        data = base64.b64decode(image.split(",", 1)[-1])
    except Exception:
        return None
    try:
        r = await _http.post(f"{comfy_host()}/upload/image",
                             files={"image": ("jarceye_src.png", data, "image/png")},
                             data={"overwrite": "true", "type": "input"}, timeout=30)
        r.raise_for_status()
        return r.json().get("name")
    except Exception:
        return None


def _comfy_graph(ckpt: str, flux: bool, prompt: str, negative: str, width: int, height: int,
                 steps: int, seed: int, cfg: float, src: str | None, denoise: float) -> dict:
    """Arma un workflow txt2img (o img2img si `src`) adaptado a FLUX/SD3 o SD/SDXL."""
    g: dict = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "", "clip": ["1", 1]}},
        "6": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": (1.0 if flux else cfg),
            "sampler_name": os.getenv("COMFY_SAMPLER", "euler"),
            "scheduler": os.getenv("COMFY_SCHEDULER", "simple"), "denoise": denoise,
            "model": ["1", 0], "positive": ["3" if flux else "2", 0],
            "negative": ["4", 0], "latent_image": None}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "jarceye"}},
    }
    if flux:
        g["3"] = {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["2", 0], "guidance": float(os.getenv("FLUX_GUIDANCE", "3.5") or 3.5)}}
    if src:
        g["10"] = {"class_type": "LoadImage", "inputs": {"image": src}}
        g["11"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["1", 2]}}
        g["6"]["inputs"]["latent_image"] = ["11", 0]
    else:
        g["5"] = {"class_type": ("EmptySD3LatentImage" if flux else "EmptyLatentImage"),
                  "inputs": {"width": width, "height": height, "batch_size": 1}}
        g["6"]["inputs"]["latent_image"] = ["5", 0]
    return g


async def comfy_generate(prompt: str, negative: str = "", width: int = 768, height: int = 768,
                         steps=None, seed: int = -1, image: str | None = None,
                         denoise: float | None = None) -> dict | None:
    """Genera (o transforma con img2img) una imagen en ComfyUI. Devuelve {b64} o {error}.
       `denoise` (img2img): 0=idéntica, 1=libre; menor = respeta más la foto original."""
    host = comfy_host()
    if not host:
        return None
    ckpt = await comfy_checkpoint()
    if not ckpt:
        return {"error": "ComfyUI sin checkpoints (revisa models/checkpoints)"}
    seed = int(seed)
    if seed < 0:
        seed = int.from_bytes(os.urandom(4), "big")
    steps = int(steps or os.getenv("COMFY_STEPS", "20") or 20)
    cfg = float(os.getenv("SD_CFG", "7") or 7)
    flux = ("flux" in ckpt.lower() or "sd3" in ckpt.lower())
    src = None
    if image:
        src = await _comfy_upload(image)
        if not src:
            return {"error": "ComfyUI: no se pudo subir la imagen base"}
        dn = float(denoise) if denoise is not None else float(os.getenv("SD_DENOISE", "0.6") or 0.6)
    else:
        dn = 1.0
    graph = _comfy_graph(ckpt, flux, prompt, negative, int(width), int(height),
                         steps, seed, cfg, src, dn)
    try:
        r = await _http.post(f"{host}/prompt", json={"prompt": graph}, timeout=30)
        r.raise_for_status()
        pid = r.json().get("prompt_id")
        if not pid:
            return {"error": "ComfyUI no aceptó el prompt"}
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"error": f"ComfyUI: {msg or ('HTTP ' + str(e.response.status_code))}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"ComfyUI: {type(e).__name__}"}
    # Espera el resultado (polling del historial). FLUX puede tardar bastante.
    for _ in range(240):  # ~360 s
        await asyncio.sleep(1.5)
        try:
            entry = (await _http.get(f"{host}/history/{pid}", timeout=10)).json().get(pid)
        except Exception:
            continue
        if not entry:
            continue
        if (entry.get("status") or {}).get("status_str") == "error":
            return {"error": "ComfyUI: el workflow falló (revisa el modelo/VRAM)"}
        for node in (entry.get("outputs") or {}).values():
            for im in (node.get("images") or []):
                if im.get("type") == "temp":
                    continue
                try:
                    v = await _http.get(f"{host}/view", params={
                        "filename": im["filename"], "subfolder": im.get("subfolder", ""),
                        "type": im.get("type", "output")}, timeout=30)
                    v.raise_for_status()
                    return {"b64": base64.b64encode(v.content).decode(), "provider": "comfyui"}
                except Exception:
                    return {"error": "ComfyUI: no se pudo descargar la imagen generada"}
    return {"error": "ComfyUI: tiempo de espera agotado"}


# Familias de modelos con VISIÓN habituales en Ollama (para autodetección).
_VISION_HINTS = ("llava", "vision", "-vl", "vl-", "qwen2.5vl", "qwen2-vl", "qwen3-vl",
                 "minicpm-v", "moondream", "bakllava", "granite3.2-vision", "gemma3",
                 "llama3.2-vision", "mistral-small3", "internvl", "cogvlm", "pixtral")


def _is_vision(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VISION_HINTS)


def _json_slice(s: str) -> str:
    """Extrae el primer bloque {...} de una respuesta (por si el modelo añade texto)."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        s = s.split("\n", 1)[-1] if s[:4].lower() == "json" else s
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if 0 <= a < b else s


async def ollama_models() -> list[str]:
    """Modelos INSTALADOS en Ollama (/api/tags)."""
    host = ollama_host()
    if not host:
        return []
    try:
        r = await _http.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
        return [m.get("name", "") for m in (r.json().get("models") or []) if m.get("name")]
    except Exception:
        return []


async def ollama_running() -> list[str]:
    """Modelos actualmente CARGADOS en memoria (Ollama /api/ps): la IA que corre ahora."""
    host = ollama_host()
    if not host:
        return []
    try:
        r = await _http.get(f"{host}/api/ps", timeout=5)
        r.raise_for_status()
        return [m.get("name", "") for m in (r.json().get("models") or []) if m.get("name")]
    except Exception:
        return []


def _prefer_running(running: list[str], vision: bool) -> str:
    """Del conjunto ya cargado en memoria, elige el más apto (visión/texto). '' si no hay."""
    if not running:
        return ""
    if vision:
        rv = [m for m in running if _is_vision(m)]
        return rv[0] if rv else ""
    rt = [m for m in running if "embed" not in m.lower()]
    return rt[0] if rt else ""


def _pick_from(models: list[str], vision: bool) -> str:
    """Elige el modelo: preferencia .env → autodetección → primero disponible."""
    env = os.getenv("OLLAMA_VISION_MODEL" if vision else "OLLAMA_MODEL", "").strip()
    if env and (not models or env in models or any(m.split(":")[0] == env for m in models)):
        return env
    if not models:
        return env  # confiamos en que exista aunque /api/tags falle
    if vision:
        vis = [m for m in models if _is_vision(m)]
        if vis:
            return vis[0]
    txt = [m for m in models if "embed" not in m.lower()]
    return (txt or models)[0]


async def pick_model(vision: bool) -> str:
    """Elige el modelo a usar, en este orden:
       1) preferencia explícita en .env (OLLAMA_MODEL / OLLAMA_VISION_MODEL)
       2) el modelo que YA esté corriendo/cargado en Ollama (/api/ps) → sin recargar
       3) autodetección entre los instalados (/api/tags)
    """
    env = os.getenv("OLLAMA_VISION_MODEL" if vision else "OLLAMA_MODEL", "").strip()
    if env:
        return env
    pref = _prefer_running(await ollama_running(), vision)
    if pref:
        return pref
    return _pick_from(await ollama_models(), vision)


async def ollama_chat(messages: list, model: str | None = None, images: list | None = None,
                      fmt: str | None = None, temperature: float = 0.2,
                      timeout: float = 90) -> str | None:
    """Chat contra Ollama (/api/chat). Adjunta imágenes (base64) al último turno de usuario."""
    host = ollama_host()
    if not host:
        return None
    mdl = model or await pick_model(bool(images))
    if not mdl:
        return None
    msgs = [dict(m) for m in messages]  # copia (no mutar el original)
    if images:
        clean = [i.split(",", 1)[-1] for i in images if i]
        for m in reversed(msgs):
            if m.get("role") == "user":
                m["images"] = clean
                break
    body = {"model": mdl, "messages": msgs, "stream": False,
            "options": {"temperature": temperature}}
    if fmt:
        body["format"] = fmt  # "json"
    try:
        r = await _http.post(f"{host}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content")
    except Exception:
        return None


# --------------------------------------------------------------------------
# IA LOCAL: llama.cpp (llama-server, OpenAI-compatible en /v1/*)
# --------------------------------------------------------------------------
def llamacpp_host() -> str:
    return (os.getenv("LLAMACPP_HOST", "http://127.0.0.1:8080") or "").rstrip("/")


def _short_model(path_or_name: str) -> str:
    """Nombre corto para mostrar: basename del gguf sin extensión."""
    n = (path_or_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return n[:-5] if n.lower().endswith(".gguf") else (n or path_or_name)


_lcpp_cache: dict = {"t": 0.0, "info": None}


async def llamacpp_info(force: bool = False) -> dict:
    """Estado del servidor llama.cpp (cacheado ~8 s):
       {up, model, vision}. Autodetecta el modelo cargado y si es multimodal."""
    now = time.monotonic()
    cached = _lcpp_cache["info"]
    if not force and cached is not None and now - _lcpp_cache["t"] < 8:
        return cached
    info = {"up": False, "model": "", "vision": False}
    host = llamacpp_host()
    if host:
        try:
            r = await _http.get(f"{host}/v1/models", timeout=5)
            r.raise_for_status()
            j = r.json()
            data = j.get("data") or []
            info["model"] = (data[0].get("id") if data else "") or ""
            caps: list = []
            for m in (j.get("models") or []):
                caps = m.get("capabilities") or caps
            info["vision"] = ("multimodal" in caps) or _is_vision(info["model"])
            info["up"] = bool(info["model"])
        except Exception:
            pass
    _lcpp_cache["t"], _lcpp_cache["info"] = now, info
    return info


async def llamacpp_chat(messages: list, model: str | None = None, images: list | None = None,
                        fmt: str | None = None, temperature: float = 0.2,
                        timeout: float = 90) -> str | None:
    """Chat contra llama.cpp (/v1/chat/completions, formato OpenAI). Visión vía image_url."""
    host = llamacpp_host()
    if not host:
        return None
    mdl = model or (await llamacpp_info())["model"] or "local"
    msgs = [dict(m) for m in messages]
    if images:
        for m in reversed(msgs):
            if m.get("role") == "user":
                txt = m.get("content") if isinstance(m.get("content"), str) else ""
                content: list = [{"type": "text", "text": txt or "Analiza esta imagen."}]
                for im in images:
                    if not im:
                        continue
                    url = im if im.startswith("data:") else f"data:image/png;base64,{im}"
                    content.append({"type": "image_url", "image_url": {"url": url}})
                m["content"] = content
                break
    body = {"model": mdl, "messages": msgs, "stream": False, "temperature": temperature}
    if fmt == "json":
        body["response_format"] = {"type": "json_object"}
    try:
        r = await _http.post(f"{host}/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content")
    except Exception:
        return None


async def local_chat(messages: list, images: list | None = None, fmt: str | None = None,
                     temperature: float = 0.2, timeout: float = 90) -> tuple[str | None, str | None]:
    """Chat contra el backend LOCAL disponible. Devuelve (texto, proveedor).
       AI_BACKEND controla la prioridad: 'auto' (por defecto) → llama.cpp si responde,
       luego Ollama; o fija 'llamacpp' / 'ollama'."""
    want_vision = bool(images)
    pref = (os.getenv("AI_BACKEND", "auto") or "auto").strip().lower()
    order = {"llamacpp": ["llamacpp"], "ollama": ["ollama"]}.get(pref, ["llamacpp", "ollama"])
    for b in order:
        if b == "llamacpp":
            info = await llamacpp_info()
            if info["up"] and (info["vision"] or not want_vision):
                out = await llamacpp_chat(messages, model=info["model"], images=images,
                                          fmt=fmt, temperature=temperature, timeout=timeout)
                if out is not None:
                    return out, "llamacpp"
        else:
            if await ollama_models():
                out = await ollama_chat(messages, images=images, fmt=fmt,
                                        temperature=temperature, timeout=timeout)
                if out is not None:
                    return out, "ollama"
    return None, None


async def sd_ready() -> bool:
    host = sd_host()
    if not host:
        return False
    try:
        r = await _http.get(f"{host}/sdapi/v1/sd-models", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def ai_health() -> dict:
    """Estado de la IA local: llama.cpp + Ollama (modelos, visión) + Stable Diffusion."""
    models = await ollama_models()
    running = await ollama_running()
    lcpp = await llamacpp_info(force=True)
    env_txt = os.getenv("OLLAMA_MODEL", "").strip()
    env_vis = os.getenv("OLLAMA_VISION_MODEL", "").strip()
    text_model = env_txt or _prefer_running(running, False) or (_pick_from(models, False) if models else "")
    vision_model = env_vis or _prefer_running(running, True) or (_pick_from(models, True) if models else "")
    pref = (os.getenv("AI_BACKEND", "auto") or "auto").strip().lower()
    if lcpp["up"] and pref in ("auto", "llamacpp"):
        active = "llamacpp"
    elif models and pref in ("auto", "ollama"):
        active = "ollama"
    elif os.getenv("OPENAI_API_KEY", ""):
        active = "openai"
    else:
        active = ""
    return {
        "active": active,
        "llamacpp": lcpp["up"],
        "llamacppHost": llamacpp_host(),
        "llamacppModel": lcpp["model"],
        "llamacppLabel": _short_model(lcpp["model"]),
        "llamacppVision": lcpp["vision"],
        "ollama": bool(models),
        "ollamaHost": ollama_host(),
        "models": models,
        "running": running,
        "visionModel": vision_model,
        "textModel": text_model,
        "hasVision": lcpp["vision"] or any(_is_vision(m) for m in models),
        "comfy": await comfy_ready(),
        "comfyHost": comfy_host(),
        "comfyModel": _short_model(await comfy_checkpoint()),
        "sd": await sd_ready(),
        "sdHost": sd_host(),
        "openai": bool(os.getenv("OPENAI_API_KEY", "")),
    }


CHAT_SYSTEM = """Eres JARC, el copiloto de IA de JARC'S EYE View, un mapa 3D de vigilancia tipo "God's Eye".
Puedes CONTROLAR el mapa y RESPONDER preguntas sobre su estado en vivo.

Recibes el ESTADO ACTUAL DEL MAPA en JSON (ubicación del centro, capas activas y conteos de entidades
visibles: vuelos, cámaras ALPR, webcams, barcos, terremotos, incendios, incidentes, vehículos...).
ÚSALO para responder. Preguntas como "cuántas cámaras hay" o "qué capas están activas" se responden con
esos números; NUNCA digas que no puedes ver el mapa: tienes el estado delante.

Responde SIEMPRE en JSON válido con esta forma exacta:
{"reply":"<respuesta en español, breve y clara>", "action": <objeto de acción o null>}

"action" ejecuta algo en el mapa; usa null si solo respondes una pregunta. Formas válidas de "action":
- {"action":"flyto","place":"<lugar>"}                         (volar a una ciudad/lugar)
- {"action":"mylocation"}                                       (ir a mi ubicación)
- {"action":"layer","layer":"planes|quakes|iss|rain|traffic|streets|ships|alpr|inc|fires|veh","on":true|false}
- {"action":"pois","kind":"parks","on":true|false}             (marcar/ocultar parques y zonas verdes de la vista)
- {"action":"scan","kind":"radio|webcams|traffic"}             (escanear emisoras/webcams/cámaras de tráfico)
- {"action":"preset","preset":"normal|crt|nvg|flir|anime|noir|snow"}
- {"action":"mapsource","source":"google3d|bing|binglabels|esri|osm"}
- {"action":"track","callsign":"<callsign>"}                   (rastrear un vuelo)
- {"action":"streetview"}                                      (Street View del punto actual)
- {"action":"generate","prompt":"<descripción en inglés>"}     (generar una imagen)
- {"action":"carfollow","orbit":true|false}                    (seguir el vehículo seleccionado; orbit=false = sin órbita)
- {"action":"cartag","tag":"<nombre>"}                         (etiquetar el vehículo; omite "tag" para que la app la pida)
- {"action":"cartrackoff"}                                     (cancelar el rastreo del vehículo)
- {"action":"daynight","mode":"day|night|off"}                 (vista de día / de noche)
- {"action":"objetivo","on":true|false}                        (modo OBJETIVO / spy zoom)
- {"action":"north"} · {"action":"hover","on":true|false}      (norte / sobrevolar el objetivo)
- {"action":"detect","kind":"people|structures|vehicles","on":true|false}  (detección en OBJETIVO)
- {"action":"optic","mode":"termal|heat|nvg|flir|noir|sar|uv|desert"}  (óptica del visor)
- {"action":"photo"} · {"action":"gallery","on":true|false}    (tomar foto / abrir-cerrar galería)
- {"action":"panels"} · {"action":"close"}                     (paneles CAPAS / cerrar ventanas)

Reglas:
- Si el usuario pide una acción (muéstrame X, activa/apaga X, vuela a X, genera X), incluye "action" y confírmalo en "reply".
- Si es una PREGUNTA sobre el estado (cuántos/qué hay/qué está activo), usa el ESTADO y responde en "reply" con "action": null.
- La capa "alpr" son las cámaras ALPR; "veh" los vehículos simulados; "inc" los incidentes de tráfico.
- Si te adjuntan una imagen del mapa, analízala en "reply".
- NO inventes datos personales reales de personas (nombres, direcciones, matrículas, teléfonos).

Ejemplos:
"cuántas cámaras hay en el mapa" -> {"reply":"En la vista hay 12 cámaras ALPR y 3 webcams.","action":null}
"muéstrame los parques de esta zona" -> {"reply":"Marcando los parques y zonas verdes de la vista.","action":{"action":"pois","kind":"parks","on":true}}
"apaga el radar de lluvia" -> {"reply":"Radar de lluvia apagado.","action":{"action":"layer","layer":"rain","on":false}}
"vuela a Tokio" -> {"reply":"Volando a Tokio.","action":{"action":"flyto","place":"Tokyo"}}"""


def _chat_parse(out: str) -> dict:
    """Convierte la salida JSON del modelo en {reply, action}. Tolera texto plano."""
    try:
        d = json.loads(_json_slice(out))
        reply = (d.get("reply") or d.get("text") or "").strip()
        action = d.get("action")
        if not isinstance(action, dict) or not action.get("action"):
            action = None
        if reply or action:
            return {"reply": reply or "Hecho.", "action": action}
    except Exception:
        pass
    return {"reply": (out or "").strip(), "action": None}


async def ai_chat(text: str, image: str | None = None, history: list | None = None,
                  context: dict | None = None) -> dict:
    """Copiloto de IA (texto/visión) consciente del mapa: responde y puede devolver una acción."""
    sys = CHAT_SYSTEM
    if context:
        try:
            sys += "\n\nESTADO ACTUAL DEL MAPA (JSON):\n" + json.dumps(context, ensure_ascii=False)[:2000]
        except Exception:
            pass
    msgs = [{"role": "system", "content": sys}]
    for h in (history or [])[-6:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:2000]})
    msgs.append({"role": "user", "content": (text or "").strip() or "Describe y analiza esta vista."})
    out, prov = await local_chat(msgs, images=[image] if image else None,
                                 fmt="json", temperature=0.3, timeout=120)
    if out:
        return {**_chat_parse(out), "provider": prov}
    key = os.getenv("OPENAI_API_KEY", "")
    if _openai_ok():
        content: list = [{"type": "text", "text": msgs[-1]["content"]}]
        if image:
            url = image if image.startswith("data:") else f"data:image/png;base64,{image}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": msgs[:-1] + [{"role": "user", "content": content}]}
        try:
            r = await _http.post("https://api.openai.com/v1/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
            r.raise_for_status()
            return {**_chat_parse(r.json()["choices"][0]["message"]["content"]), "provider": "openai"}
        except Exception:
            pass
    return {"error": "IA no disponible. Arranca tu servidor llama.cpp (llama-server) o "
                     "Ollama (ollama serve), o configura OPENAI_API_KEY."}


async def generate_image(prompt: str, negative: str = "", width: int = 768, height: int = 768,
                         steps=None, seed: int = -1, image: str | None = None) -> dict | None:
    """Genera una imagen. Prioridad: ComfyUI (workflow) → Stable Diffusion (AUTOMATIC1111) →
       OpenAI. Con `image` hace img2img."""
    if not (prompt or "").strip() and not image:
        return {"error": "escribe qué imagen generar"}
    # 1) ComfyUI (el generador que corre en COMFY_HOST, p.ej. FLUX en :8188).
    if await comfy_ready():
        d = await comfy_generate(prompt, negative, width, height, steps, seed, image)
        if d and d.get("b64"):
            return d
        # Si ComfyUI falla, intenta los siguientes; recuerda su error por si no hay otro.
        comfy_err = (d or {}).get("error")
    else:
        comfy_err = None
    # 2) Stable Diffusion AUTOMATIC1111 (si está configurado).
    host = sd_host()
    if not host or not (prompt or "").strip():
        # Respaldo: OpenAI (solo edición si hay imagen base). No llamamos a enhance_image
        # para evitar recursión: usamos el helper de edición directamente.
        if image:
            b = await _openai_edit(image, prompt or "enhance, sharpen, high detail")
            return {"b64": b} if b else {"error": comfy_err or "sin generador local (ComfyUI/SD) ni OpenAI"}
        return {"error": comfy_err or "sin generador local: arranca ComfyUI (COMFY_HOST) o configura SD_HOST"}
    payload = {
        "prompt": prompt,
        "negative_prompt": negative or "blurry, low quality, watermark, text, deformed",
        "width": int(width), "height": int(height),
        "steps": int(steps or os.getenv("SD_STEPS", "22") or 22),
        "cfg_scale": float(os.getenv("SD_CFG", "7") or 7),
        "sampler_name": os.getenv("SD_SAMPLER", "DPM++ 2M Karras"),
        "seed": int(seed),
    }
    endpoint = "/sdapi/v1/txt2img"
    if image:
        payload["init_images"] = [image.split(",", 1)[-1]]
        payload["denoising_strength"] = float(os.getenv("SD_DENOISE", "0.55") or 0.55)
        endpoint = "/sdapi/v1/img2img"
    try:
        r = await _http.post(f"{host}{endpoint}", json=payload, timeout=300)
        r.raise_for_status()
        imgs = r.json().get("images") or []
        if imgs:
            return {"b64": imgs[0], "provider": "sd"}
        return {"error": "el generador no devolvió imagen"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Stable Diffusion: {type(e).__name__}"}


async def voice_intent(text: str) -> dict | None:
    """Interpreta una orden de voz en una acción de navegación (IA local → OpenAI)."""
    text = (text or "").strip()
    if not text:
        return None
    # 1) IA LOCAL (llama.cpp o Ollama, autodetectado): salida JSON.
    out, _ = await local_chat(
        [{"role": "system", "content": VOICE_SYSTEM}, {"role": "user", "content": text}],
        fmt="json", temperature=0, timeout=25)
    if out:
        try:
            return json.loads(_json_slice(out))
        except Exception:
            pass
    # 2) Respaldo OpenAI (solo si no es modo local-only y hay clave).
    if not _openai_ok():
        return None
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": VOICE_SYSTEM},
                     {"role": "user", "content": text}],
    }
    try:
        r = await _http.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=body, timeout=20)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return None


# --------------------------------------------------------------------------
# TomTom (tráfico, etiquetas, reverse-geocode) + Google 2D roadmap
# --------------------------------------------------------------------------
async def tomtom_tile(kind: str, z: int, x: int, y: int) -> bytes | None:
    key = os.getenv("TOMTOM_KEY", "")
    if not key:
        return None
    if kind == "traffic":
        url = f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png?key={key}"
    elif kind == "labels":
        url = f"https://api.tomtom.com/map/1/tile/labels/main/{z}/{x}/{y}.png?key={key}"
    else:
        url = f"https://api.tomtom.com/map/1/tile/basic/main/{z}/{x}/{y}.png?key={key}"
    try:
        r = await _http.get(url)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


async def revgeo(lat: float, lon: float) -> dict | None:
    """Dirección/calle real de un punto (TomTom Reverse Geocoding)."""
    key = os.getenv("TOMTOM_KEY", "")
    if not key:
        return None
    try:
        r = await _http.get(f"https://api.tomtom.com/search/2/reverseGeocode/{lat},{lon}.json",
                            params={"key": key})
        a = ((r.json().get("addresses") or [{}])[0]).get("address", {})
        if a:
            return {"address": a.get("freeformAddress", ""), "street": a.get("streetName", ""),
                    "city": a.get("municipality", ""), "country": a.get("country", "")}
    except Exception:
        pass
    return None


_gmap_sessions: dict[str, dict] = {}


async def gmap_tile(z: int, x: int, y: int, map_type: str = "roadmap") -> bytes | None:
    """Tile 2D de Google (roadmap o satellite), Map Tiles API 2D con sesión cacheada por tipo."""
    key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not key:
        return None
    s = _gmap_sessions.get(map_type)
    if not (s and time.monotonic() < s["exp"] - 120):
        try:
            r = await _http.post(f"https://tile.googleapis.com/v1/createSession?key={key}",
                                 json={"mapType": map_type, "language": "en-US", "region": "US"})
            d = r.json()
            if "session" in d:
                _gmap_sessions[map_type] = {"token": d["session"], "exp": time.monotonic() + 3600 * 20}
        except Exception:
            return None
        s = _gmap_sessions.get(map_type)
    if not s:
        return None
    try:
        r = await _http.get(f"https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}",
                            params={"session": s["token"], "key": key})
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


ANALYZE_SYSTEM = """Eres un sistema de análisis de imágenes satelitales/aéreas estilo "inteligencia".
Analiza la imagen (vista cenital/oblicua del mapa) y devuelve SOLO JSON válido con datos plausibles
de estilo militar/OSINT. NO son datos reales verificados: son ESTIMACIONES de IA para una demo.

Esquema exacto:
{
 "satellite":"WORLDVIEW-3","resolution":"0.31 m","mode":"MULTI-SPECTRAL",
 "location":"<ciudad, región, país estimados>",
 "building":{"stories":"<p.ej. 1 STORY>","area":"<~X sq ft>","built":"<año est.>","use":"<Commercial / Residential / Industrial>","height":"<X ft>"},
 "roof_sections":[{"id":"ROOF SECTION A","material":"<material>","area":"<X sq ft>"}],
 "hvac":[{"id":"HVAC-1","spec":"<X Ton>"}],
 "vehicles":[{"id":"V01","plate":"<placa estimada>","make":"<marca modelo>","year":"<año>","color":"<color REAL visto>","bbox":[x,y,w,h]}],
 "structural":[{"label":"ROOF EDGE","detail":"<material>"},{"label":"WALL PANEL","detail":"..."},{"label":"WINDOW SYSTEM","detail":"..."},{"label":"PARKING LOT","detail":"..."}],
 "summary":"<1-2 frases>"
}
IMPORTANTE sobre vehículos: detecta TODOS los que realmente se vean en la imagen (hasta 14).
- "bbox" = caja del vehículo en la imagen, NORMALIZADA 0..1 como [x, y, w, h] (origen arriba-izquierda). Ajústala al vehículo lo mejor posible.
- "color" DEBE coincidir con el color real que se ve en la foto.
- La marca/modelo/año/placa son ESTIMACIONES plausibles (la placa no se puede leer: invéntala con formato de matrícula).
Cuenta también secciones de techo y unidades HVAC reales.
Si no es un edificio (campo, agua, bosque), adapta building/roof y deja arrays vacíos."""


async def analyze_scene(image: str, lat: float, lon: float) -> dict | None:
    if not image:
        return None
    # 1) VISIÓN LOCAL (llama.cpp o Ollama, modelo con visión autodetectado). Reintenta por si
    #    el JSON viene mal formado, en lugar de saltar a la nube.
    for attempt in range(2):
        vis, prov = await local_chat(
            [{"role": "system", "content": ANALYZE_SYSTEM},
             {"role": "user", "content": f"Coordenadas: {lat:.6f}, {lon:.6f}. Analiza esta vista y responde SOLO el JSON del esquema."}],
            images=[image], fmt="json", temperature=(0.4 if attempt == 0 else 0.2), timeout=180)
        if vis:
            try:
                d = json.loads(_json_slice(vis))
                d["coords"] = f"{lat:.6f}, {lon:.6f}"
                d["provider"] = prov
                rg = await revgeo(lat, lon)
                if rg:
                    d["address"] = rg.get("address", "")
                return d
            except Exception:
                continue
    # 2) Respaldo OpenAI (gpt-4o visión) — SOLO si no es modo local-only y hay clave.
    if not _openai_ok():
        return {"error": "IA de visión local no respondió. Verifica que llama.cpp (Qwen3-VL) o un "
                         "modelo con visión en Ollama esté cargado y disponible."}
    url = image if image.startswith("data:") else f"data:image/png;base64,{image}"
    body = {
        "model": "gpt-4o", "temperature": 0.4, "max_tokens": 1600,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"Coordenadas: {lat:.6f}, {lon:.6f}. Analiza esta vista."},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ]},
        ],
    }
    try:
        r = await _http.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
        r.raise_for_status()
        d = json.loads(r.json()["choices"][0]["message"]["content"])
        d["coords"] = f"{lat:.6f}, {lon:.6f}"
        rg = await revgeo(lat, lon)
        if rg:
            d["address"] = rg.get("address", "")
        return d
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"error": msg or f"OpenAI HTTP {e.response.status_code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}"}


async def _openai_edit(image: str, prompt: str) -> str | None:
    """Edición de imagen con OpenAI (gpt-image-1). Devuelve base64 o None."""
    if not _openai_ok():
        return None
    key = os.getenv("OPENAI_API_KEY", "")
    try:
        raw = base64.b64decode(image.split(",", 1)[-1])
        r = await _http.post("https://api.openai.com/v1/images/edits",
                             headers={"Authorization": f"Bearer {key}"},
                             data={"model": "gpt-image-1", "prompt": prompt, "size": "1024x1024"},
                             files={"image": ("map.png", raw, "image/png")}, timeout=180)
        r.raise_for_status()
        return r.json()["data"][0]["b64_json"]
    except Exception:
        return None


# Prompt por defecto para "Realzar esta zona": misma escena/posiciones, pero HD.
ENHANCE_PROMPT = (
    "High-definition photorealistic aerial/oblique view of this exact scene. Keep the SAME layout: "
    "same buildings, streets, cars, trees, colors and positions. Rebuild every structure crisp and "
    "sharp with high-detail architecture, clean rooftops, defined edges and realistic materials. "
    "Ultra-detailed, high resolution, natural daylight. Do NOT add, remove or move elements."
)


async def enhance_image(image: str, prompt: str = "") -> str | None:
    """'Realzar esta zona': re-renderiza la captura en alta definición conservando las
       posiciones reales. ComfyUI img2img (denoise moderado) → SD (A1111) → OpenAI edits."""
    if not image:
        return None
    p = (prompt or "").strip() or ENHANCE_PROMPT
    dn = float(os.getenv("ENHANCE_DENOISE", "0.5") or 0.5)  # conserva la composición
    # 1) ComfyUI img2img (directo, sin pasar por generate_image → sin recursión).
    if await comfy_ready():
        d = await comfy_generate(p, image=image, width=1024, height=1024, denoise=dn)
        if d and d.get("b64"):
            return d["b64"]
    # 2) Stable Diffusion A1111 (solo si hay host y ComfyUI no respondió).
    elif await sd_ready():
        d = await generate_image(p, image=image)
        if d and d.get("b64"):
            return d["b64"]
    # 3) Respaldo OpenAI (gpt-image-1 edits).
    return await _openai_edit(image, p)


_inc_cache: dict[tuple, tuple[float, list]] = {}
_INC_FIELDS = ("{incidents{type,geometry{type,coordinates},properties{iconCategory,"
               "magnitudeOfDelay,delay,events{description},from,to,roadNumbers}}}")


async def traffic_incidents(s: float, w: float, n: float, e: float) -> list[dict]:
    """Incidentes de tráfico (accidentes, obras, cierres, atascos) — TomTom."""
    key = os.getenv("TOMTOM_KEY", "")
    if not key:
        return []
    ck = (round(s, 2), round(w, 2), round(n, 2), round(e, 2))
    now = time.monotonic()
    hit = _inc_cache.get(ck)
    if hit and now - hit[0] < 60:
        return hit[1]
    out, seen = [], set()
    try:
        r = await _http.get("https://api.tomtom.com/traffic/services/5/incidentDetails",
                            params={"key": key, "bbox": f"{w},{s},{e},{n}", "fields": _INC_FIELDS,
                                    "language": "es-ES", "timeValidityFilter": "present"})
        for i in (r.json().get("incidents") or []):
            g = i.get("geometry") or {}
            p = i.get("properties") or {}
            c = g.get("coordinates")
            if not c:
                continue
            pt = c[len(c) // 2] if g.get("type") == "LineString" else c
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            cat = p.get("iconCategory", 0)
            k = (round(pt[1], 3), round(pt[0], 3), cat)   # dedup por ubicación+tipo
            if k in seen:
                continue
            seen.add(k)
            ev = (p.get("events") or [{}])[0]
            out.append({"lat": pt[1], "lon": pt[0], "cat": cat,
                        "mag": p.get("magnitudeOfDelay"), "delay": p.get("delay"),
                        "desc": ev.get("description", ""), "from": p.get("from", ""),
                        "to": p.get("to", ""), "roads": ", ".join(p.get("roadNumbers") or [])})
    except Exception:
        out = []
    _inc_cache[ck] = (now, out[:400])
    return _inc_cache[ck][1]


_alpr_cache: dict[tuple, tuple[float, list]] = {}


async def alpr_cameras(s: float, w: float, n: float, e: float) -> list[dict]:
    """Ubicaciones de cámaras ALPR/ANPR (OpenStreetMap/DeFlock vía Overpass). Datos públicos."""
    key = (round(s, 1), round(w, 1), round(n, 1), round(e, 1))
    now = time.monotonic()
    hit = _alpr_cache.get(key)
    if hit and now - hit[0] < 60:
        return hit[1]
    q = (f'[out:json][timeout:25];'
         f'(node["man_made"="surveillance"]["surveillance:type"~"ALPR|ANPR"]({s},{w},{n},{e});'
         f'way["man_made"="surveillance"]["surveillance:type"~"ALPR|ANPR"]({s},{w},{n},{e}););'
         f'out center 400;')
    out: list = []
    try:
        r = await _http.post("https://overpass-api.de/api/interpreter", data={"data": q},
                             headers={"User-Agent": "JarcsEyeView/1.0 (jarcmaster@gmail.com)"}, timeout=35)
        for el in (r.json().get("elements") or []):
            t = el.get("tags") or {}
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            out.append({"lat": lat, "lon": lon,
                        "operator": t.get("operator") or t.get("brand") or "",
                        "direction": str(t.get("direction", "")),
                        "type": t.get("surveillance:type", "ALPR")})
    except Exception:
        out = []
    _alpr_cache[key] = (now, out)
    return out


async def rain_radar() -> dict:
    """Plantilla de tiles del último frame de radar de lluvia (RainViewer, sin key)."""
    try:
        d = (await _http.get("https://api.rainviewer.com/public/weather-maps.json")).json()
        host = d.get("host")
        past = (d.get("radar") or {}).get("past") or []
        if host and past:
            return {"url": f"{host}{past[-1]['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"}
    except Exception:
        pass
    return {}


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
