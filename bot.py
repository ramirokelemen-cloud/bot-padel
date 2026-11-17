"""
Bot de turnos de pádel para ATC (atcsports.io / Alquila Tu Cancha).

Dos tipos de aviso por Telegram (los filtros están en config.toml):
  * RESUMEN (todos los días a las 9:00): turnos libres de hoy y mañana.
  * ALERTAS (cada 30 minutos): turnos que se LIBERARON desde la última
    revisión, de hoy a 6 días, empezando como mínimo dentro de 2 horas.

No hace falta tocar este archivo: toda la configuración está en config.toml.

Uso:
    python bot.py             -> revisa y avisa los turnos que se liberaron
    python bot.py --resumen   -> manda el resumen de hoy y mañana
    python bot.py --probar    -> muestra en pantalla qué turnos cumplen y
                                 manda un mensaje de prueba a Telegram
    python bot.py --loop 30   -> se queda corriendo en tu compu y revisa
                                 cada 30 minutos

Necesita dos datos de Telegram, como variables de entorno:
    TELEGRAM_TOKEN    (te lo da @BotFather)
    TELEGRAM_CHAT_ID  (tu número de usuario, te lo da @userinfobot)
"""

import hashlib
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

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


def clubes(cfg):
    return list(dict.fromkeys(limpiar_club(c) for c in cfg["clubes"] if c.strip()))


def a_minutos(hhmm):
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def huella_filtros(cfg):
    """Resumen de los filtros: si cambian, el bot se reacomoda sin mandarte avisos de más."""
    datos = {k: cfg.get(k) for k in ("duraciones", "techada", "superficies", "precio_maximo",
                                     "horarios", "anticipacion_minima_horas")}
    datos["clubes"] = sorted(clubes(cfg))
    return hashlib.sha1(json.dumps(datos, sort_keys=True).encode()).hexdigest()[:12]


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

def buscar_turnos(cfg, dias):
    """Consulta ATC (hoy + los días que siguen) y devuelve {clave: datos} de turnos que cumplen."""
    encontrados = {}
    ahora = datetime.now(ARG)
    hoy = ahora.date()
    minimo = ahora + timedelta(hours=float(cfg.get("anticipacion_minima_horas", 0)))
    for club in clubes(cfg):
        for i in range(dias):
            fecha = hoy + timedelta(days=i)
            try:
                datos = pedir_json(API.format(club=urllib.parse.quote(club), fecha=fecha.isoformat()))
            except Exception as e:
                print(f"  ! No pude consultar {club} el {fecha}: {e}")
                continue
            nombre_club = datos.get("name", club).strip()
            for cancha in datos.get("available_courts", []):
                for turno in cancha.get("available_slots", []):
                    inicio = datetime.fromisoformat(turno["start"])
                    if inicio < minimo:  # muy sobre la hora para organizar el partido
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

def nombre_dia(d):
    return DIAS[d.weekday()].capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")


def describir(t):
    inicio = datetime.fromisoformat(t["inicio"])
    techo = "techada" if t["techada"] else "descubierta"
    sup = t["superficie"].replace("blindex-", "blindex ").replace("synthetic", "sintético").replace("cement", "cemento")
    precio = f' · ${t["precio"]:,.0f}'.replace(",", ".") if t["precio"] else ""
    link = LINK.format(club=t["permalink"], fecha=t["fecha_dia"])
    return (f'🎾 <b>{t["club"]}</b> – {t["cancha"]}\n'
            f'📅 {nombre_dia(inicio)} {inicio:%d/%m} a las <b>{inicio:%H:%M}</b> ({t["duracion"]} min)\n'
            f'🏟 {techo}, {sup}{precio}\n'
            f'👉 <a href="{link}">Reservar</a>')


def agrupar(turnos):
    """Junta las canchas del mismo club, horario y duración en un solo aviso."""
    grupos = {}
    for t in turnos:
        grupos.setdefault((t["club"], t["inicio"], t["duracion"]), []).append(t)
    salida = []
    for _, lista in sorted(grupos.items(), key=lambda x: (x[0][1], x[0][0], x[0][2])):
        t = dict(lista[0])
        if len(lista) > 1:
            t["cancha"] = f'{len(lista)} canchas ({", ".join(x["cancha"] for x in lista)})'
        salida.append(t)
    return salida


def resumen_compacto(turnos):
    """Para el resumen diario: una línea por club y día con todos los horarios."""
    por_dia = {}
    for t in turnos:
        inicio = datetime.fromisoformat(t["inicio"])
        dia = date.fromisoformat(t["fecha_dia"])
        club = por_dia.setdefault(dia, {}).setdefault((t["club"], t["permalink"]), {})
        club.setdefault(f"{inicio:%H:%M}", set()).add(t["duracion"])
    bloques = []
    for dia in sorted(por_dia):
        lineas = [f"📅 <b>{nombre_dia(dia)} {dia:%d/%m}</b>"]
        for (club, permalink), horas in sorted(por_dia[dia].items()):
            link = LINK.format(club=permalink, fecha=dia.isoformat())
            detalle = ", ".join(f"{h} ({'/'.join(str(d) for d in sorted(ds))}′)" for h, ds in sorted(horas.items()))
            lineas.append(f'🎾 <a href="{link}">{club}</a>: {detalle}')
        bloques.append("\n".join(lineas))
    return bloques


def partir(texto, largo=3800):
    """Telegram acepta ~4000 caracteres por mensaje: corta entre bloques, no a la mitad."""
    partes, actual = [], ""
    for bloque in texto.split("\n\n"):
        if actual and len(actual) + len(bloque) + 2 > largo:
            partes.append(actual)
            actual = bloque
        else:
            actual = f"{actual}\n\n{bloque}" if actual else bloque
    return partes + [actual] if actual else partes


def enviar_telegram(texto, silencioso=False):
    """Manda el mensaje. Devuelve True si salió bien (reintenta hasta 4 veces)."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("  (Telegram no configurado: el mensaje solo se muestra acá)\n" + texto)
        return True
    for parte in partir(texto):
        datos = urllib.parse.urlencode({
            "chat_id": chat, "text": parte,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
            "disable_notification": "true" if silencioso else "false",
        }).encode()
        for intento in range(1, 5):
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", datos, timeout=60)
                break
            except urllib.error.HTTPError as e:
                detalle = e.read().decode(errors="ignore")
                print(f"  ! Telegram rechazó el mensaje ({e.code}): {detalle}")
                if e.code in (400, 401, 403, 404):  # token o chat id mal cargados: no tiene sentido reintentar
                    return False
            except Exception as e:
                print(f"  ! Falló el envío a Telegram (intento {intento}/4): {e}")
            time.sleep(5 * intento)
        else:
            return False
    return True


# ---------------------------------------------------------------- estado

def leer_estado():
    try:
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            estado = json.load(f)
        return estado if isinstance(estado, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def guardar_estado(estado):
    os.makedirs(os.path.dirname(ARCHIVO_ESTADO), exist_ok=True)
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=0)


# ---------------------------------------------------------------- modos

def armar_resumen(cfg, turnos=None):
    dias = int(cfg.get("dias_resumen", 2))
    hoy = datetime.now(ARG).date()
    if turnos is None:
        turnos = buscar_turnos(cfg, dias)
    else:
        limite = (hoy + timedelta(days=dias - 1)).isoformat()
        turnos = {k: t for k, t in turnos.items() if t["fecha_dia"] <= limite}
    titulo = "☀️ <b>Resumen del día</b>: turnos libres de hoy" + (" y mañana" if dias == 2 else f" y los próximos {dias - 1} días")
    if not turnos:
        return titulo + "\n\nNo hay ninguno que cumpla tus filtros. Te aviso si se libera alguno.", turnos
    texto = titulo + f" ({len(turnos)} en total, desde {hoy:%d/%m}):\n\n" + "\n\n".join(resumen_compacto(turnos.values()))
    return texto, turnos


def resumen(cfg):
    """Resumen de las 9:00. Además deja la 'foto' de arranque del día: desde las 9:15
    las alertas comparan contra esta foto, así no te repite lo que ya viste acá."""
    dias = int(cfg.get("dias_alertas", 7))
    ahora = datetime.now(ARG)
    hasta = (ahora.date() + timedelta(days=dias - 1)).isoformat()
    huella = huella_filtros(cfg)
    print(f"[{ahora:%d/%m %H:%M}] Armando el resumen diario...")
    todos = buscar_turnos(cfg, dias)
    texto, turnos = armar_resumen(cfg, todos)
    print(f"  {len(turnos)} turno(s) en el resumen.")

    # Lo que se liberó de noche para los días que el resumen no muestra: va al final,
    # así no se pierde ninguna alerta.
    anterior = leer_estado()
    if anterior and anterior.get("filtros") == huella:
        vistos = set(anterior.get("vistos", []))
        de_noche = [t for k, t in todos.items()
                    if k not in turnos and k not in vistos and t["fecha_dia"] <= anterior.get("hasta", hasta)]
        if de_noche:
            print(f"  {len(de_noche)} turno(s) liberados de noche para los días siguientes.")
            texto += ("\n\n🌙 <b>Además, durante la noche se liberaron estos turnos para los próximos días:</b>\n\n"
                      + "\n\n".join(describir(t) for t in agrupar(de_noche)))

    if not enviar_telegram(texto):
        sys.exit("No se pudo mandar el resumen a Telegram.")
    guardar_estado({"filtros": huella, "hasta": hasta, "vistos": sorted(todos),
                    "resumen": ahora.date().isoformat()})
    print("  Listo.")


def alertas(cfg):
    dias = int(cfg.get("dias_alertas", 7))
    ahora = datetime.now(ARG)
    ventana = str(cfg.get("horario_avisos", "")).strip()
    if ventana:
        desde, hasta_hora = (a_minutos(x) for x in ventana.split("-"))
        if not desde <= ahora.hour * 60 + ahora.minute <= hasta_hora:
            # Fuera de horario: no reviso ni toco la foto anterior, así lo que se libere
            # de noche te lo aviso en la primera revisión de la mañana.
            print(f"[{ahora:%d/%m %H:%M}] Fuera del horario de avisos ({ventana}): no hago nada.")
            return
    hasta = (ahora.date() + timedelta(days=dias - 1)).isoformat()
    huella = huella_filtros(cfg)
    print(f"[{ahora:%d/%m %H:%M}] Buscando turnos liberados en {len(clubes(cfg))} club(es), hasta el {hasta}...")
    anterior = leer_estado()
    if (anterior and anterior.get("resumen") != ahora.date().isoformat()
            and ahora.hour < 11 and "--resumen" not in sys.argv):
        # El resumen de las 9:00 todavía no salió (GitHub a veces se demora): lo espero,
        # así arranca el día él y las alertas siguen desde ahí.
        print(f"[{ahora:%d/%m %H:%M}] Todavía no salió el resumen de hoy: espero a la próxima revisión.")
        return
    actuales = buscar_turnos(cfg, dias)
    ok = True

    if anterior is None:
        # Primera vez: guardo lo que hay y te mando el resumen para que veas que funciona.
        print(f"  Primera corrida: {len(actuales)} turno(s) cumplen tus filtros.")
        texto, _ = armar_resumen(cfg)
        ok = enviar_telegram("✅ <b>Bot activado.</b> Cada 30 minutos te aviso si se libera un turno "
                             f"(hasta {dias - 1} días adelante y con al menos "
                             f"{cfg.get('anticipacion_minima_horas', 0)} h de anticipación), "
                             "y todos los días a las 9 te mando el resumen.\n\n" + texto)
    elif anterior.get("filtros") != huella:
        print("  Cambiaron los filtros o los clubes: me reacomodo sin mandar alertas.")
        ok = enviar_telegram("⚙️ Vi que cambiaste los filtros o los clubes. Ya está aplicado: "
                             "desde ahora te aviso con la configuración nueva.")
    else:
        vistos = set(anterior.get("vistos", []))
        hasta_antes = anterior.get("hasta", hasta)
        # "Liberado" = no estaba libre en la revisión anterior, y su día ya se estaba mirando
        # (así no te aviso de golpe todos los turnos del día nuevo que entra a medianoche).
        nuevos = [t for k, t in actuales.items() if k not in vistos and t["fecha_dia"] <= hasta_antes]
        print(f"  {len(actuales)} turno(s) libres que cumplen, {len(nuevos)} liberado(s) recién.")
        if nuevos:
            lista = agrupar(nuevos)
            titulo = ("🚨 <b>¡Se liberó un turno!</b>" if len(lista) == 1
                      else f"🚨 <b>¡Se liberaron {len(lista)} turnos!</b>")
            ok = enviar_telegram(titulo + "\n\n" + "\n\n".join(describir(t) for t in lista))
        else:
            modo = sin_tildes(str(cfg.get("sin_novedades", "silencioso")))
            if modo in ("silencioso", "con_sonido"):
                ok = enviar_telegram(f"🔎 {ahora:%H:%M} – No hay nuevos turnos disponibles.",
                                     silencioso=(modo == "silencioso"))

    if not ok:
        # No guardo el estado: así en la próxima revisión vuelve a intentar avisarte.
        sys.exit("No se pudo mandar el aviso a Telegram. Se reintenta en la próxima revisión.")
    guardar_estado({"filtros": huella, "hasta": hasta, "vistos": sorted(actuales),
                    "resumen": (anterior or {}).get("resumen")})
    print("  Listo.")


def probar(cfg):
    print("Buscando turnos que cumplen tus filtros...\n")
    texto, turnos = armar_resumen(cfg)
    print(texto.replace("<b>", "").replace("</b>", ""))
    enviar_telegram("👋 Mensaje de prueba del bot de pádel. ¡Si ves esto, Telegram funciona!")
    print("\nListo. Si configuraste Telegram, revisá tu celular.")


if __name__ == "__main__":
    config = leer_config()
    if "--resumen" in sys.argv:
        resumen(config)
    elif "--probar" in sys.argv:
        probar(config)
    elif "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        minutos = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 30
        print(f"Bot corriendo. Revisa cada {minutos} minutos. Cerrá esta ventana para frenarlo.")
        ultimo_resumen = None
        while True:
            try:
                cfg = leer_config()
                ahora = datetime.now(ARG)
                if ahora.hour >= 9 and ultimo_resumen != ahora.date():
                    resumen(cfg)
                    ultimo_resumen = ahora.date()
                alertas(cfg)
            except (Exception, SystemExit) as e:
                print("  ! Error, reintento en el próximo ciclo:", e)
            time.sleep(minutos * 60)
    else:
        alertas(config)
