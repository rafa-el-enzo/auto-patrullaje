import re, time
from pathlib import Path
from dotenv import dotenv_values
from zeep import Client, Settings
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

# === Cargar SOLO .env ===
env = dotenv_values(next(p for p in [Path.cwd()/".env", Path.cwd().parent/".env"] if p.exists()))
HOST = env["HOST"]; PORT = env["PORT"]
USER = env.get("ONVIF_USER") or env.get("USER")
PASSWORD = env.get("ONVIF_PASSWORD") or env.get("PASSWORD")
DWELL = float(env.get("DWELL_SECONDS","10"))   # 10s por defecto
SPEED = float(env.get("PTZ_SPEED","0.5"))      # 0..1

# === WSDL remotos ===
DEVICE_WSDL = "https://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl"
MEDIA_WSDL  = "https://www.onvif.org/ver10/media/wsdl/media.wsdl"
PTZ_WSDL    = "https://www.onvif.org/ver20/ptz/wsdl/ptz.wsdl"
settings = Settings(strict=False, xml_huge_tree=True)
transport = Transport(timeout=8)

xaddr_device = f"http://{HOST}:{PORT}/onvif/device_service" if PORT!="80" else f"http://{HOST}/onvif/device_service"
dev = Client(DEVICE_WSDL, settings=settings, transport=transport,
             wsse=UsernameToken(USER, PASSWORD, use_digest=True)
      ).create_service("{http://www.onvif.org/ver10/device/wsdl}DeviceBinding", xaddr_device)
caps = dev.GetCapabilities()

media = Client(MEDIA_WSDL, settings=settings, transport=transport,
               wsse=UsernameToken(USER, PASSWORD, use_digest=True)
        ).create_service("{http://www.onvif.org/ver10/media/wsdl}MediaBinding", caps.Media.XAddr)
profiles = media.GetProfiles()
assert profiles, "No hay perfiles"
profile_token = profiles[0].token

ptz = Client(PTZ_WSDL, settings=settings, transport=transport,
             wsse=UsernameToken(USER, PASSWORD, use_digest=True)
      ).create_service("{http://www.onvif.org/ver20/ptz/wsdl}PTZBinding", caps.PTZ.XAddr)

presets = ptz.GetPresets(ProfileToken=profile_token) or []

def extract_index(name: str, token: str):
    # 1) si el nombre tiene dígitos, tomamos el primero (p.ej. "Preset 12" -> 12)
    if name:
        m = re.search(r"\d+", name)
        if m:
            return int(m.group(0))
    # 2) si el token es numérico, úsalo (tu caso: '1','2','3')
    if token.isdigit():
        return int(token)
    return None

# Normalizar y ordenar
items = []
for pr in presets:
    name  = (getattr(pr,"Name","") or "").strip()
    token = (getattr(pr,"token","") or getattr(pr,"Token","")).strip()
    idx   = extract_index(name, token)
    items.append((idx, name, token))

# Filtra los que podemos ordenar por número
items_num = [(i,n,t) for (i,n,t) in items if i is not None]
if not items_num:
    # Si no pudimos sacar números de nada, usa el orden original
    items_num = [(k+1, n, t) for k,(i,n,t) in enumerate(items)]
    print("⚠️ No hay nombres/tokens numéricos; uso orden original.")
items_num.sort(key=lambda x: x[0])

print(f"Presets ordenados [{len(items_num)}]:")
for i,n,t in items_num:
    label = n if n else f"(sin nombre, token={t})"
    print(f"  {i:02d}  {label}")

print(f"\nRecorriendo cada {DWELL}s (speed={SPEED}) — cierra el Live View en la app Ezviz para no bloquear PTZ.")
try:
    while True:
        for i,n,t in items_num:
            label = n if n else f"token={t}"
            print("->", f"{i} / {label}")
            ptz.GotoPreset(
                ProfileToken=profile_token,
                PresetToken=t,
                Speed={"PanTilt":{"x":SPEED,"y":SPEED}, "Zoom":SPEED}
            )
            time.sleep(DWELL)
except KeyboardInterrupt:
    print("\nDetenido por el usuario.")
    try:
        ptz.Stop(ProfileToken=profile_token)
    except Exception:
        pass