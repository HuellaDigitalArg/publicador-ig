"""Publicador de Reels en Instagram (corre en la nube, GitHub Actions).

Cada vez que corre:
  1. Mira la hora de Argentina.
  2. Para cada provincia, busca en Drive: Provincias/<Provincia>/<fecha de hoy>/*.mp4
  3. Arma el horario del día (Buenos Aires: 8:30 + cada 40 min; resto: 12:00 y 16:00).
  4. Publica los videos cuyo horario ya pasó y que todavía no están en la cuenta
     (compara el texto con las últimas publicaciones: nunca publica dos veces).

Variables de entorno (secretos de GitHub):
  GOOGLE_SA_JSON   contenido del JSON de la cuenta de servicio de Google
  TOKENS_FILE_ID   id en Drive del archivo tokens.json:
                   {"_token": "<token usuario del sistema>",
                    "Catamarca": {"user_id": "<id cuenta IG>"}, ...}
                   (una cuenta puede tener su propio "token" si hiciera falta)
  SOLO             (opcional) publicar solo esta provincia, ej. "Catamarca"
  ENSAYO=1         (opcional) hace todo menos publicar
"""
import io, json, os, re, sys, time, tempfile, unicodedata
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

ART = timezone(timedelta(hours=-3))
PROVINCIAS_ID = "1rReTBm55NfgOaeIy3uwaV8Z7ZJ1aJBRt"
TOKENS_ID = os.environ.get("TOKENS_FILE_ID") or "13oOkjd7BaENficOW2Bvkqs5pPDIA4QhN"
# Token de "usuario del sistema" del portfolio (no vence) -> API de Graph de Facebook
IG = "https://graph.facebook.com/v23.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v23.0"
ENSAYO = os.environ.get("ENSAYO") == "1"
# Carpeta de Drive -> cuenta de Instagram donde se publica (si no es la misma)
CUENTA_DE = {"ciudaddebuenosaires": "Buenos Aires"}  # CABA y provincia de BA -> @huelladigitalbuenosaires (publicaciones separadas)
SOLO = os.environ.get("SOLO", "").strip()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------- Horario ----------------
def horario(provincia, cantidad):
    """Devuelve la lista de horas (datetime ART de hoy) para N videos."""
    hoy = datetime.now(ART).replace(second=0, microsecond=0)
    if norm(provincia) == "buenosaires":
        base = hoy.replace(hour=8, minute=30)
        return [base + timedelta(minutes=40 * i) for i in range(cantidad)]
    fijos = [(12, 0), (16, 0)]
    horas = [hoy.replace(hour=h, minute=m) for h, m in fijos]
    # Si algún día vienen más de 2 videos, los extra van cada 2 h desde las 18
    extra = [hoy.replace(hour=18, minute=0) + timedelta(hours=2 * i)
             for i in range(max(0, cantidad - len(fijos)))]
    return (horas + extra)[:cantidad]


# ---------------- Drive ----------------
def drive():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    cred = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=cred, cache_discovery=False)


def listar(dv, padre):
    out, token = [], None
    while True:
        r = dv.files().list(q=f"'{padre}' in parents and trashed=false",
                            fields="nextPageToken, files(id,name,mimeType,shortcutDetails)",
                            pageSize=200, pageToken=token,
                            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return out


def real_id(f):
    """Los archivos de la carpeta son accesos directos: devuelve el id real."""
    if f["mimeType"] == "application/vnd.google-apps.shortcut":
        return f["shortcutDetails"]["targetId"]
    return f["id"]


def bajar(dv, file_id, destino):
    req = dv.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destino, "wb") as fh:
        d = MediaIoBaseDownload(fh, req, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = d.next_chunk()


def leer_texto(dv, file_id):
    buf = io.BytesIO()
    d = MediaIoBaseDownload(buf, dv.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = d.next_chunk()
    t = buf.getvalue().decode("utf-8-sig").strip()
    return t.replace("\\#", "#")


def cargar_tokens(dv):
    return json.loads(leer_texto(dv, TOKENS_ID))


def guardar_tokens(dv, tokens):
    data = json.dumps(tokens, ensure_ascii=False, indent=2).encode()
    dv.files().update(fileId=TOKENS_ID,
                      media_body=MediaIoBaseUpload(io.BytesIO(data), "application/json"),
                      supportsAllDrives=True).execute()


# ---------------- Instagram ----------------
def base(token):
    """Token de Instagram Login (IGAA...) -> graph.instagram.com; de sistema -> graph.facebook.com"""
    return "https://graph.instagram.com/v23.0" if token.startswith("IG") else IG


def ig_get(path, token, **params):
    r = requests.get(f"{base(token)}/{path}", params={**params, "access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json()


def ya_publicado(user_id, token, caption):
    """True si alguna de las últimas 25 publicaciones tiene el mismo texto."""
    clave = norm(caption)[:80]
    data = ig_get(f"{user_id}/media", token, fields="caption", limit=25).get("data", [])
    return any(norm(m.get("caption", ""))[:80] == clave for m in data)


def publicar_reel(user_id, token, video_path, caption):
    # 1) Contenedor con subida directa (no hace falta link público)
    r = requests.post(f"{base(token)}/{user_id}/media", data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": caption, "share_to_feed": "true", "access_token": token}, timeout=60)
    r.raise_for_status()
    cid = r.json()["id"]
    # 2) Subir el archivo
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as fh:
        u = requests.post(f"{RUPLOAD}/{cid}", data=fh, timeout=1800, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    u.raise_for_status()
    # 3) Esperar a que Instagram procese el video (hasta 15 min)
    for _ in range(90):
        st = ig_get(cid, token, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            break
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rechazó el video: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram tardó demasiado en procesar el video")
    # 4) Publicar
    p = requests.post(f"{base(token)}/{user_id}/media_publish",
                      data={"creation_id": cid, "access_token": token}, timeout=120)
    p.raise_for_status()
    return p.json()["id"]


def refrescar_token(token):
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


# ---------------- Principal ----------------
def main():
    ahora = datetime.now(ART)
    hoy = ahora.strftime("%Y-%m-%d")
    log(f"Corrida {ahora:%Y-%m-%d %H:%M} ART {'(ENSAYO)' if ENSAYO else ''}")
    dv = drive()
    tokens = cargar_tokens(dv)
    tokens_cambiaron = False
    errores = 0

    for prov in sorted(listar(dv, PROVINCIAS_ID), key=lambda f: f["name"]):
        if prov["mimeType"] != "application/vnd.google-apps.folder":
            continue
        nombre = prov["name"]
        if SOLO and norm(SOLO) != norm(nombre):
            continue
        nombre_cuenta = CUENTA_DE.get(norm(nombre), nombre)
        cuenta = next((v for k, v in tokens.items()
                       if not k.startswith("_") and norm(k) == norm(nombre_cuenta)), None)
        if cuenta and not cuenta.get("token"):
            cuenta = {**cuenta, "token": tokens.get("_token"), "sistema": True}
        if not cuenta or not cuenta.get("token") or not cuenta.get("user_id"):
            log(f"[{nombre}] sin cuenta conectada, salteo")
            continue
        carpeta = next((f for f in listar(dv, prov["id"]) if f["name"] == hoy), None)
        if not carpeta:
            log(f"[{nombre}] no hay carpeta {hoy}")
            continue
        archivos = listar(dv, carpeta["id"])
        videos = sorted((f for f in archivos if f["name"].lower().endswith(".mp4")),
                        key=lambda f: f["name"])
        textos = {f["name"]: f for f in archivos if f["name"].endswith("_caption.txt")}
        horas = horario(nombre, len(videos))

        # Token del usuario del sistema: no vence, no hay nada que renovar.
        # Token propio de la cuenta (caso raro): renovar una vez por semana.
        if not cuenta.get("sistema") and time.time() - cuenta.get("refrescado", 0) > 7 * 86400:
            try:
                cuenta["token"] = refrescar_token(cuenta["token"])
                cuenta["refrescado"] = int(time.time())
                tokens_cambiaron = True
                log(f"[{nombre}] token renovado")
            except Exception as e:
                log(f"[{nombre}] no pude renovar el token: {e}")

        # Solo uno por corrida y por cuenta, para respetar el espaciado
        for video, hora in zip(videos, horas):
            if hora > ahora:
                break
            txt = textos.get(video["name"][:-4] + "_caption.txt")
            if not txt:
                log(f"[{nombre}] {video['name']} no tiene _caption.txt, lo salteo")
                continue  # sin texto no se puede verificar si ya se publicó
            caption = leer_texto(dv, real_id(txt))
            try:
                if ya_publicado(cuenta["user_id"], cuenta["token"], caption):
                    log(f"[{nombre}] {video['name']} ya está publicado")
                    continue
                log(f"[{nombre}] toca {video['name']} (horario {hora:%H:%M})")
                if ENSAYO:
                    break
                with tempfile.TemporaryDirectory() as tmp:
                    ruta = os.path.join(tmp, "v.mp4")
                    bajar(dv, real_id(video), ruta)
                    mid = publicar_reel(cuenta["user_id"], cuenta["token"], ruta, caption)
                log(f"[{nombre}] PUBLICADO {video['name']} -> media {mid}")
            except Exception as e:
                errores += 1
                log(f"[{nombre}] ERROR con {video['name']}: {e}")
            break

    if tokens_cambiaron:
        guardar_tokens(dv, tokens)
    if errores:
        sys.exit(1)  # GitHub marca la corrida en rojo y manda mail


if __name__ == "__main__":
    main()
