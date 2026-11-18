"""Sidecar web-GUI voor iSponsorBlockTV. Past de originele container niet aan."""
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import docker
from docker.errors import APIError, NotFound
from flask import Flask, Response, jsonify, render_template, request

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
TARGET = os.environ.get("TARGET_CONTAINER", "isponsorblocktv")
IMAGE_REPO = os.environ.get("IMAGE_REPO", "ghcr.io/dmunozv04/isponsorblocktv")
GUI_PASSWORD = os.environ.get("GUI_PASSWORD", "")

CATEGORIES = ["sponsor", "selfpromo", "exclusive_access", "interaction",
              "intro", "outro", "preview", "filler", "music_offtopic", "hook"]
PRESET_TAGS = ["latest", "close-ads-5s"]

app = Flask(__name__)
client = docker.from_env()
file_lock = threading.Lock()
log_buffer = deque(maxlen=500)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---------- helpers ----------
def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def load_stats():
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("total", {"muted": 0, "skipped_ads": 0, "segments": 0})
    s.setdefault("days", {})
    return s


stats = load_stats()


def bump(kind):
    with file_lock:
        stats["total"][kind] += 1
        day = stats["days"].setdefault(today(), {"muted": 0, "skipped_ads": 0, "segments": 0})
        day[kind] += 1
        # bewaar max 60 dagen
        for old in sorted(stats["days"])[:-60]:
            del stats["days"][old]
        atomic_write(STATS_PATH, stats)


AD_SKIP = re.compile(r"skipping ad|ad cannot be skipped|scheduling ad dance", re.I)
MUTE = re.compile(r"(?<![a-z])muting", re.I)
SEGMENT = re.compile(r"skipping segment", re.I)


def classify(line):
    msg = line.split(" - ", 3)[-1]
    if SEGMENT.search(msg):
        return "segments"
    if AD_SKIP.search(msg):
        return "skipped_ads"
    if MUTE.search(msg):
        return "muted"
    return None


# ---------- log collector ----------
def handle_line(line, count=True):
    line = ANSI.sub("", line).rstrip()
    if not line:
        return
    log_buffer.append(line)
    if count:
        kind = classify(line)
        if kind:
            bump(kind)


def collector():
    since = int(time.time())
    prefilled = None
    while True:
        try:
            c = client.containers.get(TARGET)
            if c.status != "running":
                time.sleep(3)
                continue
            if prefilled != c.id:
                # Eerdere logregels inlezen voor het dashboard, zonder ze mee te tellen.
                prefilled = c.id
                old = c.logs(tail=300).decode("utf-8", "replace").splitlines()
                for l in old:
                    handle_line(l, count=False)
                since = int(time.time())
            buf = b""
            for chunk in c.logs(stream=True, follow=True, since=since, tail=0):
                since = int(time.time())
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    handle_line(raw.decode("utf-8", "replace"))
            time.sleep(2)  # stream beëindigd (container gestopt/herstart)
        except (NotFound, APIError, Exception):
            time.sleep(5)


threading.Thread(target=collector, daemon=True, name="log-collector").start()


# ---------- config ----------
def read_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(mute_ads, skip_ads, categories):
    with file_lock:
        cfg = read_config()  # alleen deze velden aanpassen; rest blijft intact
        with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        cfg["mute_ads"] = bool(mute_ads)
        cfg["skip_ads"] = bool(skip_ads)
        cfg["skip_categories"] = [c for c in categories if c in CATEGORIES]
        atomic_write(CONFIG_PATH, cfg)


def modify_devices(fn):
    """Pas config['devices'] aan, bewaar een back-up en herstart de container."""
    with file_lock:
        cfg = read_config()
        with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        cfg["devices"] = fn(list(cfg.get("devices", [])))
        atomic_write(CONFIG_PATH, cfg)
    client.containers.get(TARGET).restart(timeout=15)


async def _pair(code):
    from pyytlounge import YtLoungeApi
    async with YtLoungeApi("iSponsorBlockTV") as api:
        if not await api.pair(code):
            raise ValueError("Koppelen mislukt: controleer de code op de tv.")
        name = getattr(api, "screen_name", None) or "YouTube on TV"
        return api.auth.screen_id, name


# ---------- online-status per tv (alleen lezen) ----------
avail = {}


async def _check_all(devices):
    from pyytlounge import YtLoungeApi
    for d in devices:
        sid = d.get("screen_id")
        if not sid:
            continue
        try:
            async with YtLoungeApi("iSponsorBlockTV-GUI") as api:
                await api.pair_with_screen_id(sid, d.get("name"))
                avail[sid] = await api.is_available()
        except Exception:
            avail[sid] = None


def availability_loop():
    import asyncio
    while True:
        try:
            asyncio.run(_check_all(read_config().get("devices", [])))
        except Exception:
            pass
        time.sleep(30)


threading.Thread(target=availability_loop, daemon=True, name="availability").start()


# ---------- container ----------
def container_info():
    try:
        c = client.containers.get(TARGET)
    except NotFound:
        return {"exists": False}
    image_ref = c.attrs["Config"]["Image"]
    started = c.attrs["State"].get("StartedAt")
    uptime = None
    if c.status == "running" and started:
        dt = datetime.fromisoformat(started[:26].rstrip("Z") + "+00:00")
        uptime = int((datetime.now(timezone.utc) - dt).total_seconds())
    return {"exists": True, "status": c.status, "image": image_ref,
            "tag": image_ref.rsplit(":", 1)[1] if ":" in image_ref.split("/")[-1] else "latest",
            "image_id": c.attrs["Image"][7:19], "uptime": uptime}


def device_status(cfg):
    """Koppel logregels aan tv's. De logger heet iSponsorBlockTV-<id>."""
    import hashlib
    lines = list(log_buffer)
    devices = cfg.get("devices", [])
    logger_re = re.compile(r"iSponsorBlockTV-([0-9A-Za-z_-]+)")
    seen = []
    for l in lines:
        m = logger_re.search(l)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))

    assigned = {}
    for i, d in enumerate(devices):
        sid = d.get("screen_id") or ""
        cands = {sid, hashlib.sha256(sid.encode()).hexdigest()} if sid else set()
        for lid in seen:
            if lid in cands:
                assigned[i] = lid
    free_ids = [x for x in seen if x not in assigned.values()]
    free_devs = [i for i in range(len(devices)) if i not in assigned]
    if len(free_ids) == 1 and len(free_devs) == 1:
        assigned[free_devs[0]] = free_ids[0]

    out = []
    for i, d in enumerate(devices):
        name = d.get("name") or d.get("screen_id", "?")
        lid = assigned.get(i)
        mine = [l for l in lines if lid and ("iSponsorBlockTV-" + lid) in l]
        last = mine[-1] if mine else ""
        low = last.lower()
        connected = bool(last) and not any(w in low for w in ("disconnect", "lost", "error", "failed"))
        video = None
        for l in reversed(mine):
            m = re.search(r"(?:Playing video|Getting segments for next video:)\s+([\w-]{11})\b", l)
            if m:
                video = m.group(1)
                break
        shown = last.split(" - ", 3)[-1] if last else ""
        ago = None
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", last)
        if m:
            try:
                ago = int((datetime.now() - datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")).total_seconds())
            except ValueError:
                pass
        out.append({"screen_id": d.get("screen_id"), "name": name, "connected": connected,
                    "online": avail.get(d.get("screen_id")), "ago": ago,
                    "video": video, "last": shown[-160:]})
    return out


def recreate(target_tag, force=False):
    old = client.containers.get(TARGET)
    a = old.attrs
    new_ref = f"{IMAGE_REPO}:{target_tag}"
    old_ref = a["Config"]["Image"]
    client.images.pull(IMAGE_REPO, tag=target_tag)
    new_img = client.images.get(new_ref)
    if not force and new_img.id == a["Image"] and new_ref == old_ref:
        return {"updated": False, "message": f"Geen nieuwe versie voor '{target_tag}'. Je draait al de nieuwste."}

    hc = a["HostConfig"]
    kwargs = dict(
        name=TARGET, detach=True,
        environment=a["Config"].get("Env") or [],
        volumes=hc.get("Binds") or [],
        network_mode=hc.get("NetworkMode", "host"),
        restart_policy=hc.get("RestartPolicy") or {"Name": "unless-stopped"},
        labels=a["Config"].get("Labels") or {},
    )
    old_image_id = a["Image"]
    old.stop(timeout=15)
    old.remove()
    try:
        client.containers.run(new_ref, **kwargs)
    except Exception:
        client.containers.run(old_image_id, **kwargs)  # rollback
        raise
    return {"updated": True, "message": f"Container opnieuw aangemaakt met {new_ref}."}


# ---------- auth ----------
@app.before_request
def auth():
    if not GUI_PASSWORD:
        return None
    a = request.authorization
    if a and a.username == "admin" and a.password == GUI_PASSWORD:
        return None
    return Response("Login vereist", 401, {"WWW-Authenticate": 'Basic realm="iSponsorBlockTV"'})


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html", categories=CATEGORIES, presets=PRESET_TAGS)


@app.route("/api/status")
def api_status():
    try:
        cfg = read_config()
    except Exception as e:
        cfg = {}
        cfg_error = str(e)
    else:
        cfg_error = None
    with file_lock:
        s = json.loads(json.dumps(stats))
    day = s["days"].get(today(), {"muted": 0, "skipped_ads": 0, "segments": 0})
    return jsonify({
        "container": container_info(),
        "stats": {"total": s["total"], "today": day},
        "config": {"mute_ads": cfg.get("mute_ads", False), "skip_ads": cfg.get("skip_ads", False),
                   "skip_categories": cfg.get("skip_categories", [])},
        "config_error": cfg_error,
        "devices": device_status(cfg),
        "logs": list(log_buffer)[-30:],
    })


@app.route("/api/config", methods=["POST"])
def api_config():
    d = request.get_json(force=True)
    try:
        save_config(d.get("mute_ads"), d.get("skip_ads"), d.get("skip_categories", []))
        client.containers.get(TARGET).restart(timeout=15)
        return jsonify({"ok": True, "message": "Opgeslagen en herstart."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/devices/remove", methods=["POST"])
def api_dev_remove():
    sid = (request.get_json(force=True).get("screen_id") or "")
    try:
        modify_devices(lambda ds: [d for d in ds if d.get("screen_id") != sid])
        return jsonify({"ok": True, "message": "Tv ontkoppeld en herstart."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/devices/rename", methods=["POST"])
def api_dev_rename():
    d = request.get_json(force=True)
    sid, name = d.get("screen_id"), (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "Naam is leeg."}), 400

    def fn(ds):
        for x in ds:
            if x.get("screen_id") == sid:
                x["name"] = name
        return ds
    try:
        modify_devices(fn)
        return jsonify({"ok": True, "message": "Naam aangepast en herstart."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/devices/add", methods=["POST"])
def api_dev_add():
    import asyncio
    d = request.get_json(force=True)
    code = re.sub(r"\D", "", d.get("code") or "")
    if len(code) < 6:
        return jsonify({"ok": False, "message": "Vul de tv-code in (cijfers)."}), 400
    try:
        sid, auto_name = asyncio.run(_pair(code))
        name = (d.get("name") or "").strip() or auto_name

        def fn(ds):
            if any(x.get("screen_id") == sid for x in ds):
                raise ValueError("Deze tv is al gekoppeld.")
            ds.append({"screen_id": sid, "name": name, "offset": 0})
            return ds
        modify_devices(fn)
        return jsonify({"ok": True, "message": f"'{name}' gekoppeld en herstart."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    d = request.get_json(force=True)
    tag = (d.get("tag") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        return jsonify({"ok": False, "message": "Ongeldige tag."}), 400
    try:
        return jsonify({"ok": True, **recreate(tag, bool(d.get("force")))})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8095)
