"""
Bot de turnos de pádel para ATC (atcsports.io / Alquila Tu Cancha).

Revisa los turnos libres de los clubes que elijas y te manda un mensaje
de Telegram cuando se libera uno que cumple tus filtros (config.toml).

No hace falta tocar este archivo: toda la configuración está en config.toml.

Uso:
    python bot.py            -> revisa una vez y avisa lo nuevo (lo usa GitHub)
    python bot.py --probar   -> muestra en pantalla qué turnos cumplen hoy y
                                manda un mensaje de prueba a Telegram
    python bot.py --loop 5   -> se queda corriendo y revisa cada 5 minutos
                                (para dejarlo en tu propia compu)

Necesita dos datos de Telegram, como variables de entorno:
    TELEGRAM_TOKEN    (te lo da @BotFather)
    TELEGRAM_CHAT_ID  (tu número de usuario, te lo da @userinfobot)
"""

import json
import os
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://atcsports.io/api/v3/availability/sportclubs/{club}?date={fecha}"
LINK = "https://atcsports.io/venues/{club}?dia={fecha}"
PADEL = "7"  # id de deporte de pádel en ATC
ARG = timezone(timedelta(hours=-3))

CARPETA = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CONFIG = os.path.join(CARPETA, "config.toml")
ARCHIVO_ESTADO = os.path.join(CARPETA, "estado", "vistos.json")

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
SUPERFICIES = {
    "sintetico": "synthetic",
    "cemento": "cement",
    "blindex": "blindex",
    "alfombra": "carpet",
}


# ---------------------------------------------------------------- utilidades

def leer_config():
    with open(ARCHIVO_CONFIG, "rb") as f:
        return tomllib.load(f)


def sin_tildes(texto):
    return (texto.lower().replace("á", "a").replace("é", "e").replace("í", "i")
            .replace("ó", "o").replace("ú", "u"))


def limpiar_club(texto):
    """Acepta el link completo de ATC o solo el nombre del club en el link.
    https://atcsports.io/venues/tiger-padel-lanus?dia=...  ->  tiger-padel-lanus"""
    texto = texto.strip()
    if "/venues/" in texto:
        texto = texto.split("/venues/", 1)[1]
    return texto.split("?")[0].split("#")[0].strip("/")


def a_minutos(hhmm):
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def pedir_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (bot-turnos-padel)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ---------------------------------------------------------------- filtros

def cumple(cancha, turno, fecha_dia, cfg):
    """Devuelve True si el turno pasa todos los filtros de config.toml."""
    if PADEL not in [str(s) for s in cancha.get("sport_ids", [PADEL])]:
        return False

    horarios = {sin_tildes(k): v for k, v in cfg.get("horarios", {}).items()}
    rango = str(horarios.get(DIAS[fecha_dia.weekday()], "")).strip()
    if not rango:  # día sin horario = no avisar
        return False
    desde, hasta = (a_minutos(x) for x in rango.split("-"))

    inicio = datetime.fromisoformat(turno["start"])
    minutos = inicio.hour * 60 + inicio.minute
    if inicio.date() > fecha_dia:  # turnos pasada la medianoche (00:00, 00:30...)
        minutos += 24 * 60
    if not desde <= minutos <= hasta:
        return False

    duraciones = cfg.get("duraciones", [])
    if duraciones and turno.get("duration") not in duraciones:
        return False

    techada = sin_tildes(str(cfg.get("techada", "da_igual")))
    if techada == "si" and not cancha.get("is_roofed"):
        return False
    if techada == "no" and cancha.get("is_roofed"):
        return False

    superficies = cfg.get("superficies", [])
    if superficies:
        tipo = (cancha.get("surface_type") or "").lower()
        if not any(SUPERFICIES.get(sin_tildes(s), sin_tildes(s)) in tipo for s in superficies):
            return False

    maximo = cfg.get("precio_maximo", 0)
    precio = (turno.get("price") or {}).get("cents", 0) / 100
    if maximo and precio > maximo:
        return False

    return True


# ---------------------------------------------------------------- búsqueda

def buscar_turnos(cfg):
    """Consulta ATC y devuelve un diccionario {clave: datos} de turnos que cumplen."""
    encontrados = {}
    ahora = datetime.now(ARG)
    hoy = ahora.date()
    for club in dict.fromkeys(limpiar_club(c) for c in cfg["clubes"] if c.strip()):
        for i in range(int(cfg.get("dias_adelante", 7))):
            fecha = hoy + timedelta(days=i)
            try:
                datos = pedir_json(API.format(club=urllib.parse.quote(club), fecha=fecha.isoformat()))
            except Exception as e:
                print(f"  ! No pude consultar {club} el {fecha}: {e}")
                continue
            nombre_club = datos.get("name", club)
            for cancha in datos.get("available_courts", []):
                for turno in cancha.get("available_slots", []):
                    inicio = datetime.fromisoformat(turno["start"])
                    if inicio <= ahora:
                        continue
                    if not cumple(cancha, turno, fecha, cfg):
                        continue
                    clave = f'{club}|{cancha["id"]}|{turno["start"]}|{turno.get("duration")}'
                    encontrados[clave] = {
                        "club": nombre_club,
                        "permalink": datos.get("permalink", club),
                        "fecha_dia": fecha.isoformat(),
                        "cancha": cancha.get("name", ""),
                        "techada": bool(cancha.get("is_roofed")),
                        "superficie": cancha.get("surface_type") or "",
                        "inicio": turno["start"],
                        "duracion": turno.get("duration"),
                        "precio": (turno.get("price") or {}).get("cents", 0) / 100,
                    }
            time.sleep(0.5)  # para no saturar la página
    return encontrados


# ---------------------------------------------------------------- mensajes

def describir(t):
    inicio = datetime.fromisoformat(t["inicio"])
    dia = DIAS[inicio.weekday()].capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")
    techo = "techada" if t["techada"] else "descubierta"
    sup = t["superficie"].replace("blindex-", "blindex ").replace("synthetic", "sintético").replace("cement", "cemento")
    precio = f' · ${t["precio"]:,.0f}'.replace(",", ".") if t["precio"] else ""
    link = LINK.format(club=t["permalink"], fecha=t["fecha_dia"])
    return (f'🎾 <b>{t["club"]}</b> – {t["cancha"]}\n'
            f'📅 {dia} {inicio:%d/%m} a las <b>{inicio:%H:%M}</b> ({t["duracion"]} min)\n'
            f'🏟 {techo}, {sup}{precio}\n'
            f'👉 <a href="{link}">Reservar</a>')


def agrupar(turnos):
    """Junta las canchas del mismo club y horario en un solo aviso."""
    grupos = {}
    for t in turnos:
        grupos.setdefault((t["club"], t["inicio"], t["duracion"]), []).append(t)
    salida = []
    for (_, inicio, _), lista in sorted(grupos.items(), key=lambda x: x[0][1]):
        t = dict(lista[0])
        if len(lista) > 1:
            t["cancha"] = f'{len(lista)} canchas ({", ".join(x["cancha"] for x in lista)})'
        salida.append(t)
    return salida


def enviar_telegram(texto):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("  (Telegram no configurado: el mensaje solo se muestra acá)\n" + texto)
        return
    for i in range(0, len(texto), 3800):  # Telegram corta en ~4000 caracteres
        datos = urllib.parse.urlencode({
            "chat_id": chat, "text": texto[i:i + 3800],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", datos, timeout=30)


# ---------------------------------------------------------------- estado

def leer_estado():
    try:
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def guardar_estado(claves):
    os.makedirs(os.path.dirname(ARCHIVO_ESTADO), exist_ok=True)
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(sorted(claves), f, indent=0)


# ---------------------------------------------------------------- principal

def revisar(cfg):
    print(f"[{datetime.now(ARG):%d/%m %H:%M}] Revisando {len(cfg['clubes'])} club(es)...")
    actuales = buscar_turnos(cfg)
    anteriores = leer_estado()

    if anteriores is None:  # primera vez
        print(f"  Primera corrida: {len(actuales)} turno(s) cumplen tus filtros.")
        if cfg.get("avisar_lo_que_ya_esta_libre", True):
            if actuales:
                lista = agrupar(actuales.values())
                enviar_telegram("✅ <b>Bot activado.</b> Esto ya está libre ahora:\n\n"
                                + "\n\n".join(describir(t) for t in lista[:25])
                                + ("\n\n(y más...)" if len(lista) > 25 else "")
                                + "\n\nDesde ahora te aviso solo cuando se libere algo nuevo.")
            else:
                enviar_telegram("✅ <b>Bot activado.</b> Ahora no hay turnos que cumplan tus filtros. "
                                "Te aviso apenas se libere uno.")
    else:
        nuevos = [actuales[k] for k in actuales if k not in set(anteriores)]
        print(f"  {len(actuales)} turno(s) libres que cumplen, {len(nuevos)} nuevo(s).")
        if nuevos:
            lista = agrupar(nuevos)
            titulo = "🚨 <b>¡Se liberó un turno!</b>" if len(lista) == 1 else f"🚨 <b>¡Se liberaron {len(lista)} turnos!</b>"
            enviar_telegram(titulo + "\n\n" + "\n\n".join(describir(t) for t in lista))

    guardar_estado(actuales.keys())


def probar(cfg):
    print("Buscando turnos que cumplen tus filtros...\n")
    actuales = buscar_turnos(cfg)
    lista = agrupar(actuales.values())
    if not lista:
        print("Ahora no hay ninguno libre que cumpla (es normal si son horarios muy buscados).")
    for t in lista:
        print(describir(t).replace("<b>", "").replace("</b>", "")
              .split("\n👉")[0] + "\n")
    enviar_telegram(f"👋 Mensaje de prueba del bot de pádel. Encontré {len(lista)} horario(s) "
                    "que hoy cumplen tus filtros. ¡Si ves esto, Telegram funciona!")
    print("\nListo. Si configuraste Telegram, revisá tu celular.")


if __name__ == "__main__":
    config = leer_config()
    if "--probar" in sys.argv:
        probar(config)
    elif "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        minutos = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 5
        print(f"Bot corriendo. Revisa cada {minutos} minutos. Cerrá esta ventana para frenarlo.")
        while True:
            try:
                revisar(leer_config())
            except Exception as e:
                print("  ! Error, reintento en el próximo ciclo:", e)
            time.sleep(minutos * 60)
    else:
        revisar(config)
