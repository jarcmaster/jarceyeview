# JARC's EYE View — Rastreador de vuelos en vivo

> **Autor:** JOSE RODRIGUEZ, Computer Engineer

Panel de monitoreo en tiempo real sobre un globo 3D fotorrealista
(**CesiumJS + Google Photorealistic 3D Tiles**), con backend en **Python (FastAPI)**
que empuja datos por **WebSocket**.

```
OpenSky (ADS-B) ─┐
adsbdb (rutas)  ─┤→ FastAPI (WebSocket) → Navegador (CesiumJS + Google 3D Tiles)
Telegram (avisos)┘
```

## Captura

**Panel de control** (rastreo de vuelos y enlace a Google Earth):

![Panel de control](assets/panel-control.png)

## Funcionalidades

- **Aviones en vivo** de la zona que estás viendo (OpenSky Network).
- **#1 Estelas + movimiento fluido**: cada avión se interpola entre consultas
  (dead-reckoning) y deja una estela; se ve como un radar, no a saltos.
- **#2 Filtros**: por país, aerolínea/callsign, altitud mínima, solo emergencias,
  y toggle de estelas.
- **#3 Alertas de emergencia**: squawk 7500/7600/7700 → aviso a Telegram + avión en rojo.
- **#4 Origen → destino** de cada vuelo (adsbdb) al seleccionarlo.
- **Rastreo de un vuelo**: selecciona un avión → "Rastrear + Telegram".
  Recibes avisos a **30, 20, 15, 10 y 5 min** del aterrizaje y **al aterrizar**.
  Se dibuja el aeropuerto destino y una línea hacia él, con ETA en vivo.
- **Vuela a tu ubicación** al abrir (geolocalización del navegador, con respaldo por IP).
- **Scanner de radios** 📻: escanea emisoras de internet cercanas a la zona del mapa
  (Radio Browser), las muestra como marcadores y en una lista; clic → **reproduce** el stream.
- **Estado de vuelo** (AviationStack): terminal, gate, hora programada/real de aterrizaje
  y delays, en el panel de selección (botón «Horario / Gate») y en los avisos de Telegram.
- **Webcams en vivo** 📷 (Windy): escanea webcams cercanas a la zona del mapa, marcadores
  en el globo y lista; clic → reproductor en vivo incrustado + enlace a Windy.
  Incluye toggle **«🚦 Solo cámaras de tráfico»** (cámaras de carretera en vivo).
- **Búsqueda con pin + Street View**: al buscar un lugar cae un pin y un botón abre Street View.
- **Capas globales** (panel CAPAS): **aviones**, **terremotos** en vivo (USGS), **ISS** con estela
  (wheretheiss.at), **radar de lluvia** (RainViewer), **barcos (AIS)** en vivo por WebSocket
  (AISStream, `AISSTREAM_KEY`) y **cámaras ALPR** (ubicaciones públicas de OpenStreetMap/DeFlock).
- **Dock de control** (barra inferior): **presets visuales** (Normal/CRT/NVG/FLIR/Anime/Noir/Snow),
  **fuentes de mapa** (Google 3D / Bing / ESRI / OSM) y **comandos de voz** con GPT.
- **Comandos de voz**: el navegador transcribe (Web Speech API) y GPT (backend, `OPENAI_API_KEY`)
  interpreta la orden en una acción: volar a un lugar, activar capas, cambiar preset/mapa,
  rastrear un vuelo, Street View, etc. La API key vive solo en el backend.
- **Paneles plegables**: clic en el encabezado de cada panel de la izquierda para contraer/expandir.
- **Modo OBJETIVO** (spy zoom): arrastra un recuadro y hace un zoom cinematográfico estilo satélite,
  con HUD de datos en vivo (lat/lon/área/elevación/dirección real), viewfinder de cámara con
  glitch/TV-flicker, autofocus, reticle anclado al punto y efecto "enhance".
- **Live Environment** (IA): al acercarte a <100 ft (o desde el dock) captura la vista y GPT-4o
  la analiza (edificio, techos, HVAC, vehículos, estructura) en un modal estilo inteligencia,
  con animación de "satélite recibiendo feed" y generación de vistas con gpt-image-1.
- **TomTom** (`TOMTOM_KEY`, proxied): **tráfico en vivo**, **nombres de calles**, **direcciones
  reales** (reverse-geocode) e **incidentes de tráfico** (accidentes/obras/cierres/atascos con
  descripción y retraso). Fuentes de mapa extra: **Google Normal** (2D) y **Street View**.
- **🚗 Rastreo y simulación de vehículos**: miles de autos que circulan **sobre las calles reales**
  (vías de OpenStreetMap/Overpass), con **tamaño real** por coche visible en vista oblicua y
  altura muestreada de la superficie 3D (no flotan ni van off-road). Icono **fotorrealista
  top-down** generado por ComfyUI según color/marca/año/tipo, con versión de **noche con faros**
  y **animación de marcha**. Buscador por **placa/VIN**, seguimiento de **múltiples vehículos**,
  cámara suavizada y **ORBITAL FEED** con telemetría en vivo. Toggle de etiquetas.
- **🧠 IA local (privada, sin nube)**: voz, visión, chat de asistente y generación de imágenes
  corren en tu máquina — **llama.cpp** o **Ollama** (texto/visión, autodetecta el modelo cargado)
  y **ComfyUI**/**Stable Diffusion** (FLUX/SD/SDXL/SD3) para imágenes. Modo **solo local**
  (`AI_LOCAL_ONLY=1`): nunca usa OpenAI; OpenAI queda solo como respaldo opcional.
- **Asistente IA con contexto del mapa**: responde el estado y **ejecuta acciones** (volar a un
  lugar, activar capas, cambiar preset/mapa, rastrear vuelo/vehículo, controlar el HUD).
- **Voz — micro de escucha continua**: dicta comandos sin pulsar; lo no reconocido pasa al
  asistente IA. Incluye comandos para el vehículo seguido y los controles del HUD.
- **Calles fiables**: los nombres/geometría de calles se piden **por el backend** (sin CORS ni
  rate-limit del navegador), con **caché en disco 30 días**, respaldo vía la **API principal de
  OSM** y *circuit breaker*; los Overpass públicos quedan como último recurso. Soporta Overpass
  local con `OVERPASS_URL`. En pantalla se ve el indicador **CALLES vs modo LIBRE**.

## 1. Requisitos

- Python 3.11+ (probado con 3.14)
- **API key de Google Maps Platform** con la **Map Tiles API** habilitada.
- (Opcional pero recomendado) cuenta OpenSky para más cuota.
- (Opcional) bot de Telegram para las notificaciones.

Todo lo demás es **opcional**: cada servicio se activa solo si pones su clave. La app
arranca sin ninguna clave (globo base de Cesium, aviones anónimos, IA local, etc.).

## 1.1 Claves y tokens — dónde conseguir cada uno

Todas las claves se leen de variables de entorno (`.env`) o se editan desde el panel
(**<http://localhost:8000/panel>** → gestor de API keys). **Ninguna clave sale al navegador**:
el backend hace de proxy. Las marcadas *(reinicio)* requieren reiniciar el servidor tras cambiarlas.

| Servicio | Variable(s) | Para qué | Dónde obtenerla |
|---|---|---|---|
| **Google Maps** *(reinicio)* | `GOOGLE_MAPS_API_KEY` | Google 3D Tiles fotorrealistas, mapa 2D y Street View | <https://console.cloud.google.com/google/maps-apis> → habilita **Map Tiles API** (+ Maps JavaScript API para 2D/Street View) → *Credenciales → Crear clave de API* |
| **Cesium Ion** *(reinicio)* | `CESIUM_ION_TOKEN` | Capas base / terreno de Cesium | <https://cesium.com/ion/tokens> (cuenta gratuita → *Access Tokens*) |
| **OpenSky** *(reinicio)* | `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | Más cuota de posiciones de aviones (evita HTTP 429) | <https://opensky-network.org/> → crea cuenta → *Account → API clients* → **Create OAuth2 client** |
| **Telegram** *(reinicio)* | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Avisos de rastreo de vuelos | Habla con **@BotFather** → `/newbot` (token); el `chat_id` con el paso de la sección 4 |
| **AviationStack** | `AVIATIONSTACK_KEY` | Estado de vuelo: terminal, gate, hora real, delays | <https://aviationstack.com/> → *Sign Up Free* → panel → *API Access Key* |
| **Windy Webcams** | `WINDY_WEBCAMS_KEY` | Webcams en vivo cercanas al mapa | <https://api.windy.com/keys> (regístrate y crea una **Webcams API** key) |
| **TomTom** | `TOMTOM_KEY` | Tráfico en vivo, nombres de calles, reverse-geocode e incidentes | <https://developer.tomtom.com/> → *Register* → *Dashboard → My Keys* |
| **AISStream** *(reinicio)* | `AISSTREAM_KEY` | Barcos (AIS) en vivo por WebSocket | <https://aisstream.io/> → *Sign up* → *API Keys → Create* |
| **NASA FIRMS** | `FIRMS_MAP_KEY` | Focos de incendios activos | <https://firms.modaps.eosdis.nasa.gov/api/map_key/> (introduce tu email → recibes el MAP_KEY) |
| **OpenAI** *(respaldo)* | `OPENAI_API_KEY`, `OPENAI_MODEL` | Solo si NO usas IA local: voz, asistente e imágenes | <https://platform.openai.com/api-keys> → *Create new secret key* |

### IA local (sin claves — solo host/modelo)

No necesitan API key; son servicios que corren en tu equipo. Configúralos por host:

| Servicio | Variable(s) | Por defecto |
|---|---|---|
| **Ollama** (texto/visión) | `OLLAMA_HOST`, `OLLAMA_MODEL`, `OLLAMA_VISION_MODEL` | `http://localhost:11434` (modelo: autodetectado) |
| **llama.cpp** (llama-server) | `LLAMACPP_HOST` | `http://127.0.0.1:8080` |
| **ComfyUI** (imágenes) | `COMFY_HOST` | `http://127.0.0.1:8188` |
| **Stable Diffusion** (AUTOMATIC1111) | `SD_HOST` | `http://localhost:7860` |
| **IA — modo solo local** | `AI_LOCAL_ONLY`, `AI_BACKEND` | `1` (no usa OpenAI); backend `auto` |

### Servicios SIN clave (no hay que hacer nada)

OpenSky anónimo (aviones), adsbdb (rutas), OpenStreetMap/Overpass/Nominatim (calles),
Radio Browser (radios), USGS (terremotos), wheretheiss.at (ISS), RainViewer (lluvia),
ArcGIS/OSM (mapas base) e ip-api.com (ubicación por IP).

## 2. Instalación

```powershell
cd D:\jarceye
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
copy .env.example .env      # edita .env con tus claves
```

## 3. Ejecutar

```powershell
py -m uvicorn backend.main:app --reload --port 8000
```

Hay **dos formas** de ver el globo:

### Opción A — Google Earth Pro (escritorio) ⭐ recomendada si lo tienes instalado

1. Abre el **panel de control**: <http://localhost:8000/panel>
2. Pulsa **«Abrir JarcsEyeView.kml en Google Earth»** (descarga <code>JarcsEyeView.kml</code>).
3. Ábrelo con Google Earth Pro (doble clic). Aparece en *Lugares temporales*.
4. Muévete/haz zoom: Google Earth le pide al servidor los aviones del **área que ves**
   y se refresca solo cada pocos segundos (por `NetworkLink`).
5. Para **rastrear un vuelo**: en el panel escribe su *callsign* y pulsa «Rastrear».
   El avión y su ruta al aeropuerto destino se dibujan en Google Earth, y llegan los
   avisos a Telegram (30/20/15/10/5 min y aterrizaje).

Cómo funciona por dentro:
- `GET /earth.kml`  → archivo maestro con el `NetworkLink` (ábrelo una vez).
- `GET /flights.kml?BBOX=w,s,e,n` → KML dinámico; Google Earth añade el `BBOX` de tu vista.
- `GET /panel` + `POST /api/track` `/api/untrack` + `GET /api/status` → control del rastreo.

### Opción B — Navegador (CesiumJS + Google 3D Tiles)

Abre <http://localhost:8000>. Necesita `GOOGLE_MAPS_API_KEY`; sin ella arranca con el
globo base de Cesium. Toda la interacción (filtros, selección, rastreo) está en la web.

## 4. Configurar Telegram (para los avisos de vuelo)

1. En Telegram habla con **@BotFather** → `/newbot` → copia el **TOKEN**.
2. Envíale un mensaje a tu bot recién creado.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el `chat.id`.
4. Pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`.

Sin Telegram el rastreo funciona igual (verás el ETA en pantalla), solo que no
llegan los mensajes.

## 5. Cómo usarlo

1. Mueve/haz zoom: el backend consulta OpenSky **solo para el área visible**.
2. Clic en un avión → panel de **Selección** con datos y su **ruta**.
3. Botón **"Rastrear + Telegram"** → empiezan los avisos por umbrales.
4. Panel **Rastreo activo**: ruta, distancia, ETA y los umbrales que se van cumpliendo.
   Botón para detener.

## 6. Notas y límites

- **Cuota de OpenSky anónima es baja.** Si ves `⚠ OpenSky HTTP 429`, añade
  `OPENSKY_CLIENT_ID/SECRET` en `.env` (cuenta gratuita → app OAuth2).
- El **ETA es estimado** (distancia al destino ÷ velocidad); no modela patrones
  de aproximación ni esperas, pero es suficiente para los avisos.
- Las **rutas** vienen de adsbdb (comunitario): la mayoría de vuelos comerciales
  las tiene; algunos privados/militares no.

## 7. Arquitectura del código

- `backend/services.py` — clientes externos: `OpenSky`, `RouteService` (adsbdb),
  `Telegram`, y `haversine_km`.
- `backend/main.py` — FastAPI + WebSocket. Tareas `poller` (aviones del área) y
  `tracker` (seguimiento de un vuelo, ETA, umbrales y notificaciones).
- `frontend/index.html` — CesiumJS: render, interpolación, estelas, filtros,
  paneles de selección y rastreo.

Mensajes WebSocket documentados al inicio de `backend/main.py`.

## 8. Licencia

Distribuido bajo licencia **MIT**. Ver [`LICENSE`](LICENSE).

## Fuentes de datos

- Posiciones ADS-B: [OpenSky Network](https://opensky-network.org)
- Rutas de vuelo: [adsbdb](https://www.adsbdb.com)
- Globo 3D: [CesiumJS](https://cesium.com) · Google Photorealistic 3D Tiles · Google Earth Pro
