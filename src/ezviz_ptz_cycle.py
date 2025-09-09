import time, re
from pathlib import Path
from typing import Any, Optional, List
from dotenv import dotenv_values
from requests import Session
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError
from zeep import Client, Settings
from zeep.cache import SqliteCache
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken
from zeep.helpers import serialize_object

# ================== CARGAR .env (solo archivo) ==================
envp = next(p for p in [Path.cwd()/".env", Path.cwd().parent/".env", Path.cwd().parents[2]/".env"] if p.exists())
cfg = dotenv_values(envp)

def as_bool(v: Any, default=True) -> bool:
    if v is None: return default
    s = str(v).strip().lower()
    if s in ("1","true","yes","on"):  return True
    if s in ("0","false","no","off"): return False
    return default

HOST = cfg.get("HOST")
PORT = cfg.get("PORT")
USER = cfg.get("ONVIF_USER") or cfg.get("USER")
PASSWORD = cfg.get("ONVIF_PASSWORD") or cfg.get("PASSWORD")
assert HOST and PORT and USER and PASSWORD, "Faltan HOST/PORT/USER/PASSWORD en .env"

# Tuning / flags
DWELL                = float(cfg.get("DWELL_SECONDS", "10"))     # ventana por preset para buscar detección
PERSON_CLEAR_SECONDS = float(cfg.get("PERSON_CLEAR_SECONDS", "8"))  # ausencia sostenida para “libre”
EVENT_POLL_SECONDS   = float(cfg.get("EVENT_POLL_SECONDS", "1")) # frecuencia de PullMessages
IDLE_HOLD            = float(cfg.get("IDLE_HOLD_SECONDS", "3"))  # usado solo en respaldo MoveStatus
MOVE_TMO             = float(cfg.get("MOVE_FINISH_TIMEOUT","12"))
SPEED                = float(cfg.get("PTZ_SPEED","0.5"))         # 0..1
USE_EVENTS           = as_bool(cfg.get("USE_EVENTS","1"), True)
DEBUG                = as_bool(cfg.get("DEBUG","1"), True)

EVENT_KEYWORDS = [s.strip() for s in (cfg.get("EVENT_KEYWORDS","Human,People,Person,Motion,MotionAlarm,CellMotionDetector").split(",")) if s.strip()]

# ================== WSDL + transporte con CACHÉ & RETRY ==================
DEVICE_WSDL = "https://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl"
MEDIA_WSDL  = "https://www.onvif.org/ver10/media/wsdl/media.wsdl"
PTZ_WSDL    = "https://www.onvif.org/ver20/ptz/wsdl/ptz.wsdl"
EVENTS_WSDL = "https://www.onvif.org/ver10/events/wsdl/event.wsdl"

cache_path = Path(cfg.get("CACHE_PATH") or (Path.home()/".cache"/"zeep_cache.db"))
cache_path.parent.mkdir(parents=True, exist_ok=True)

session = Session()
session.headers["User-Agent"] = "onvif-zeep-client/1.0 (+friendly)"
retry = Retry(
    total=6, backoff_factor=1.0,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=["GET","HEAD","OPTIONS"]
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

settings  = Settings(strict=False, xml_huge_tree=True)
transport = Transport(timeout=12, cache=SqliteCache(path=str(cache_path), timeout=30*24*3600), session=session)

if DEBUG:
    print(f"[cache] usando {cache_path}")
    print(f"[env] HOST={HOST} PORT={PORT} USER={USER} USE_EVENTS={USE_EVENTS}")

# ================== Servicios ONVIF base ==================
xaddr_device = f"http://{HOST}:{PORT}/onvif/device_service" if PORT!="80" else f"http://{HOST}/onvif/device_service"

dev = Client(DEVICE_WSDL, settings=settings, transport=transport,
             wsse=UsernameToken(USER, PASSWORD, use_digest=True)
      ).create_service("{http://www.onvif.org/ver10/device/wsdl}DeviceBinding", xaddr_device)
caps = dev.GetCapabilities()
if DEBUG:
    print("[caps] Media:", getattr(getattr(caps,'Media',None),'XAddr',None))
    print("[caps] PTZ  :", getattr(getattr(caps,'PTZ',None),'XAddr',None))
    print("[caps] Events:", getattr(getattr(caps,'Events',None),'XAddr',None))

media = Client(MEDIA_WSDL, settings=settings, transport=transport,
               wsse=UsernameToken(USER, PASSWORD, use_digest=True)
        ).create_service("{http://www.onvif.org/ver10/media/wsdl}MediaBinding",
                         caps.Media.XAddr)
profiles = media.GetProfiles()
assert profiles, "No hay perfiles"
profile_token = profiles[0].token

ptz = Client(PTZ_WSDL, settings=settings, transport=transport,
             wsse=UsernameToken(USER, PASSWORD, use_digest=True)
      ).create_service("{http://www.onvif.org/ver20/ptz/wsdl}PTZBinding",
                       caps.PTZ.XAddr)

# ================== Events (PullPoint) con fallback ==================
events_xaddr = getattr(getattr(caps,'Events',None),'XAddr',None)
have_events  = bool(events_xaddr) and USE_EVENTS
pullpoint = None

def topic_matches(topic_str: str) -> bool:
    t = (topic_str or "").lower()
    return any(k.lower() in t for k in EVENT_KEYWORDS)

def normalize_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool): return v
    s = str(v).strip().lower()
    if s in ("true","1","yes","on"):  return True
    if s in ("false","0","no","off"): return False
    try:
        return float(s) > 0
    except Exception:
        return None

if have_events:
    try:
        ev_client = Client(EVENTS_WSDL, settings=settings, transport=transport,
                           wsse=UsernameToken(USER, PASSWORD, use_digest=True))
        ev = ev_client.create_service(
            "{http://www.onvif.org/ver10/events/wsdl}EventPortType",
            events_xaddr
        )
        sub = ev.CreatePullPointSubscription()
        pull_addr = getattr(getattr(sub, "SubscriptionReference", None), "Address", None)
        if hasattr(pull_addr, "_value_1"):
            pull_addr = pull_addr._value_1
        if not pull_addr:
            print("⚠️  Events: no hubo PullPoint Address; desactivo Events.")
            have_events = False
        else:
            pp_client = Client(EVENTS_WSDL, settings=settings, transport=transport,
                               wsse=UsernameToken(USER, PASSWORD, use_digest=True))
            pullpoint = pp_client.create_service(
                "{http://www.onvif.org/ver10/events/wsdl}PullPointSubscriptionBinding",
                pull_addr
            )
            if DEBUG: print("✅ Events PullPoint OK (cache activo)")
    except HTTPError as e:
        print(f"⚠️  Events/CreatePullPoint falló: {e} → usar respaldo por MoveStatus.")
        have_events = False
    except Exception as e:
        print(f"⚠️  Events error: {e} → usar respaldo por MoveStatus.")
        have_events = False

def pull_detection(timeout_s: float, limit: int = 10) -> Optional[bool]:
    """True = detección presente; False = explícitamente no; None = sin info."""
    if not have_events or not pullpoint:
        return None
    try:
        dur = f"PT{max(1,int(round(timeout_s)))}S"
        msgs = pullpoint.PullMessages(Timeout=dur, MessageLimit=limit)
        nm = getattr(msgs, "NotificationMessage", None) or []
        if not isinstance(nm, list): nm = [nm]
        detected_any = None
        for m in nm:
            d = serialize_object(m)
            # Topic
            topic = ""
            try:
                topic = str(((d.get("Topic") or {}).get("_value_1")) or d.get("Topic") or "")
            except Exception:
                topic = str(d.get("Topic") or "")
            if not topic_matches(topic):
                continue
            # Buscar bandera booleana en SimpleItem(s)
            truthy = None
            stack: List[Any] = [d.get("Message", {})]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if "Value" in cur and normalize_bool(cur["Value"]) is not None:
                        truthy = normalize_bool(cur["Value"])
                    for v in cur.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(cur, list):
                    stack.extend(cur)
            if truthy is not None:
                detected_any = truthy if detected_any is None else (detected_any or truthy)
                if DEBUG: print(f"[event] topic={topic} detected={truthy}")
        return detected_any
    except Exception as e:
        if DEBUG: print(f"[event] PullMessages error: {e}")
        return None

# ================== Respaldo por MoveStatus ==================
def read_status():
    try:
        return ptz.GetStatus(ProfileToken=profile_token)
    except Exception:
        return None

def move_state(st) -> str:
    try:
        ms = getattr(st, "MoveStatus", None)
        pan = str(getattr(ms, "PanTilt", "")).upper()
        zoom = str(getattr(ms, "Zoom", "")).upper()
        if "MOVING" in (pan, zoom): return "MOVING"
        if "IDLE"   in (pan, zoom): return "IDLE"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"

def wait_move_finish(timeout=MOVE_TMO):
    if DEBUG: print(f"[move] Esperando fin (tmo={timeout}s)")
    t0 = time.time()
    while time.time()-t0 < timeout:
        time.sleep(0.3)
        st = read_status()
        state = move_state(st)
        if DEBUG: print(f"[move] t=+{time.time()-t0:4.1f}s state={state}")
        if state == "IDLE":
            return True
    return False

# ================== Presets: ordenar por número ==================
def num_from_name_or_token(name: str, token: str) -> Optional[int]:
    if name:
        m = re.search(r"\d+", name)
        if m: return int(m.group(0))
    if token and token.isdigit():
        return int(token)
    return None

presets = ptz.GetPresets(ProfileToken=profile_token) or []
items = []
for pr in presets:
    name  = (getattr(pr,"Name","") or "").strip()
    token = (getattr(pr,"token","") or getattr(pr,"Token","")).strip()
    idx = num_from_name_or_token(name, token)
    if idx is not None:
        items.append((idx, name, token))
items.sort(key=lambda x: x[0])

print("Presets en orden:")
for i,n,t in items:
    print(f"  {i:02d}  {(n or '(sin nombre)')} token={t}")
print(f"\nModo: patrulla hasta detectar PERSONA; cuando hay detección, esperar ausencia por {PERSON_CLEAR_SECONDS}s seguidos.")
print(f"Params: dwell={DWELL}s, speed={SPEED}, events={'ON' if have_events else 'OFF'}")

# ================== Lógica principal ==================
def track_until_clear():
    """Con Events: espera hasta que no haya detección durante PERSON_CLEAR_SECONDS seguidos.
       Sin Events: usa MoveStatus=IDLE mantenido por IDLE_HOLD s."""
    if have_events and pullpoint:
        if DEBUG: print(f"[track] Detección → esperando ausencia {PERSON_CLEAR_SECONDS}s")
        last_det = time.time()  # empezamos asumiendo “hay detección”
        while True:
            res = pull_detection(EVENT_POLL_SECONDS)
            now = time.time()
            if res is True:
                last_det = now
            # res False = no detección explícita → no resetea last_det
            # res None  = sin info → tampoco resetea
            if now - last_det >= PERSON_CLEAR_SECONDS:
                if DEBUG: print("[track] Ausencia sostenida → reanudo patrulla")
                return
    else:
        if DEBUG: print(f"[track-fallback] sin Events → MoveStatus(IDLE {IDLE_HOLD}s)")
        stable_for = 0.0
        while True:
            time.sleep(0.5)
            state = move_state(read_status())
            idle = (state == "IDLE")
            stable_for = (stable_for + 0.5) if idle else 0.0
            if DEBUG: print(f"[track-fallback] state={state:7s} stable={stable_for:3.1f}s")
            if stable_for >= IDLE_HOLD:
                return

try:
    while True:
        for i,n,t in items:
            label = n or f"token={t}"
            print(f"\n-> Preset {i}: {label}")
            ptz.GotoPreset(ProfileToken=profile_token, PresetToken=t,
                           Speed={"PanTilt":{"x":SPEED,"y":SPEED}, "Zoom":SPEED})
            wait_move_finish()

            # ventana de búsqueda: si aparece detección, nos quedamos hasta limpiar
            start = time.time()
            detected = False
            while time.time() - start < DWELL:
                res = pull_detection(EVENT_POLL_SECONDS)  # True/False/None
                if res is True:
                    detected = True
                    break
                if res is None:
                    # respaldo: si no hay Events, usa MOVING como indicio
                    if move_state(read_status()) == "MOVING":
                        detected = True
                        break
                time.sleep(0.1)

            # Solo activar seguimiento en los últimos dos presets (3 y 4)
            if detected and i >= len(items) - 2:  # Si es uno de los últimos dos presets
                print(f"[detect] Detección en preset {i} - verificando si es continua")
                
                # Verificar que la detección sea continua por al menos 2 segundos
                confirmation_start = time.time()
                confirmed = False
                while time.time() - confirmation_start < 2:
                    res = pull_detection(EVENT_POLL_SECONDS)
                    if res is True:
                        confirmed = True
                    else:
                        confirmed = False
                        break
                    time.sleep(0.2)
                
                if confirmed:
                    print(f"[detect] Detección confirmada en preset {i} - iniciando seguimiento")
                    last_detection = time.time()
                    
                    while True:
                        res = pull_detection(EVENT_POLL_SECONDS)
                        now = time.time()
                        
                        if res is True:
                            last_detection = now
                        elif now - last_detection > PERSON_CLEAR_SECONDS:
                            print(f"[detect] No hay detección por {PERSON_CLEAR_SECONDS}s - continuando patrulla")
                            break
                            
                        time.sleep(EVENT_POLL_SECONDS)
except KeyboardInterrupt:
    print("\nDetenido por el usuario.")
    try: ptz.Stop(ProfileToken=profile_token)
    except Exception: pass
