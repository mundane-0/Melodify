#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Melodify - backend local : recherche multi-source, dedoublonnage, playlists,
   paroles, prefs/last persistes, telechargements robustes (N ouvriers + watchdog),
   centre de telechargement + bouton vider (i18n). Zero dependance Google."""
import sys, os, re, json, uuid, time, random, threading, webbrowser
from pathlib import Path
from urllib.parse import unquote
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, request, Response, jsonify, abort, send_file

BASE   = Path(__file__).resolve().parent
CACHE  = BASE / "cache"; COVERS = CACHE / "covers"; LIBF = BASE / "library.json"
PREFSF = BASE / "prefs.json"; LASTF = BASE / "last.json"
for d in (CACHE, COVERS): d.mkdir(parents=True, exist_ok=True)
PORT = 7777
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DL_WORKERS = 6
MAX_TRIES  = 4

lock = threading.Lock()
def load_lib():
    if LIBF.exists():
        try:
            lib = json.loads(LIBF.read_text(encoding="utf-8")); lib.setdefault("tracks", {}); lib.setdefault("playlists", {}); return lib
        except Exception: pass
    return {"tracks": {}, "playlists": {}}
def save_lib(lib):
    tmp = LIBF.with_suffix(".tmp"); tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(tmp, LIBF)
LIB = load_lib()
def load_prefs():
    if PREFSF.exists():
        try: return json.loads(PREFSF.read_text(encoding="utf-8"))
        except Exception: pass
    return {}
def save_prefs_file(d):
    tmp = PREFSF.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, PREFSF)
def _load_last():
    if LASTF.exists():
        try: return json.loads(LASTF.read_text(encoding="utf-8"))
        except Exception: pass
    return {}
def _save_last(d):
    tmp = LASTF.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, LASTF)

def norm(s):
    s = (s or "").lower(); s = re.sub(r"\(.*?\)|\[.*?\]", " ", s); s = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿñæœ ]", "", s); return re.sub(r"\s+", " ", s).strip()
def make_key(t, a): return norm(t) + " — " + norm(a)
def new_id(): return uuid.uuid4().hex[:12]
def backoff(k): return min(60, 4 * (2 ** (k - 1))) + random.uniform(0, 3)
def file_for(tid):
    t = LIB["tracks"].get(tid, {}); f = t.get("file")
    if f:
        p = CACHE / f
        if p.exists(): return p
    for p in CACHE.glob(f"{tid}.*"):
        if p.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg"): return p
    return None
def fetch_cover(tid, url):
    if not url: return
    dest = COVERS / f"{tid}.jpg"
    if dest.exists(): return
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.status_code == 200 and len(r.content) > 500: dest.write_bytes(r.content)
    except Exception: pass
def duration_of(path):
    try:
        from mutagen import File; m = File(str(path)); return int(m.info.length) if m and m.info else 0
    except Exception: return 0
def find_existing(title, artist):
    k = make_key(title, artist)
    with lock:
        for tid, t in LIB["tracks"].items():
            if make_key(t.get("title", ""), t.get("artist", "")) == k: return tid
    return None
def create_track(title, artist, album, cover, query):
    ex = find_existing(title, artist)
    if ex: return ex, True
    tid = new_id()
    with lock:
        LIB["tracks"][tid] = {"id": tid, "title": title, "artist": artist, "album": album or "", "cover": cover or "",
            "duration": 0, "file": None, "status": "downloading", "query": query or f"{artist} {title}",
            "added": int(time.time()), "fav": False, "tries": 0, "next_try": 0, "last_error": ""}; save_lib(LIB)
    return tid, False

jobs, dl_queue, dl_lock = {}, [], threading.Lock()
recent = deque(maxlen=8); global_pause_until = 0.0
def queue_download(tid):
    with dl_lock:
        if tid in jobs and jobs[tid]["status"] in ("queued", "downloading"): return
        jobs[tid] = {"percent": 0, "status": "queued", "error": None, "cancel": False}; dl_queue.append(tid)
def _cancel_one(tid):
    with dl_lock:
        try: dl_queue.remove(tid)
        except ValueError: pass
        jobs[tid] = {"percent": 0, "status": "cancelled", "error": None, "cancel": True}
    with lock:
        t = LIB["tracks"].get(tid)
        if t and t.get("status") in ("queued", "downloading", "error"):
            t["status"] = "cancelled"; t["next_try"] = 0; t["file"] = None; save_lib(LIB)
    for f in CACHE.glob(f"{tid}.*"):
        if f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg"): f.unlink(missing_ok=True)
def _hook(tid, d):
    if d.get("status") == "downloading":
        try: pct = float(str(d.get("_percent_str", "0")).strip().replace("%", "") or 0)
        except Exception: pct = 0
        with dl_lock:
            if tid in jobs: jobs[tid]["percent"] = pct
def worker():
    global global_pause_until
    while True:
        if time.time() < global_pause_until: time.sleep(2); continue
        with dl_lock:
            tid = dl_queue.pop(0) if dl_queue else None
            if tid is not None: jobs[tid]["status"] = "downloading"
        if tid is None: time.sleep(0.3); continue
        t = LIB["tracks"].get(tid)
        if not t: continue
        ok = False
        try:
            import subprocess as _sp
            out = str(CACHE / f"{tid}.%(ext)s")
            try:
                _r = _sp.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f", "bestaudio/best",
                              "-o", out, "ytsearch1:" + t["query"]], capture_output=True, timeout=120)
            except _sp.TimeoutExpired:
                raise RuntimeError("timeout : titre bloque, retente plus tard")
            if _r.returncode != 0:
                raise RuntimeError((_r.stderr or b"").decode("utf-8", "ignore")[-200:].strip() or "yt-dlp a echoue")
            p = file_for(tid)
            if not p: raise RuntimeError("fichier introuvable apres telechargement")
            with dl_lock: cancelled = bool(jobs.get(tid, {}).get("cancel"))
            with lock:
                if cancelled or LIB["tracks"].get(tid, {}).get("status") == "cancelled":
                    LIB["tracks"][tid]["status"] = "cancelled"; LIB["tracks"][tid]["file"] = None; save_lib(LIB); drop = True
                else:
                    LIB["tracks"][tid]["file"] = p.name; LIB["tracks"][tid]["duration"] = duration_of(p); LIB["tracks"][tid]["status"] = "ready"; save_lib(LIB); drop = False
            if drop:
                for f in CACHE.glob(f"{tid}.*"):
                    if f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg"): f.unlink(missing_ok=True)
                with dl_lock: jobs.pop(tid, None); recent.append(True)
                continue
            with dl_lock: jobs[tid] = {"percent": 100, "status": "done", "error": None}; ok = True
        except Exception as e:
            with lock:
                tr = LIB["tracks"].get(tid, {}); tries = tr.get("tries", 0) + 1
                LIB["tracks"][tid]["status"] = "error"; LIB["tracks"][tid]["tries"] = tries
                LIB["tracks"][tid]["last_error"] = str(e)[:160]; LIB["tracks"][tid]["next_try"] = (time.time() + backoff(tries)) if tries < MAX_TRIES else 0; save_lib(LIB)
            with dl_lock: jobs[tid] = {"percent": 0, "status": "error", "error": str(e)[:160]}
        with dl_lock:
            recent.append(ok)
            if len(recent) >= 6 and list(recent)[-6:].count(False) >= 5: global_pause_until = time.time() + 40; recent.clear()
def retry_scheduler():
    while True:
        time.sleep(3)
        if time.time() < global_pause_until: continue
        now = time.time()
        with lock:
            todo = [tid for tid, t in LIB["tracks"].items() if t.get("status") == "error" and t.get("tries", 0) < MAX_TRIES and now >= t.get("next_try", 0)]
            for tid in todo: LIB["tracks"][tid]["status"] = "downloading"; LIB["tracks"][tid]["last_error"] = ""
            if todo: save_lib(LIB)
        for tid in todo: queue_download(tid)
def recover_on_boot():
    for f in CACHE.glob("*.part"): f.unlink(missing_ok=True)
    ids = []
    with lock:
        for tid, t in list(LIB["tracks"].items()):
            if t.get("status") == "cancelled": LIB["tracks"].pop(tid, None); continue
            if t.get("status") in ("downloading", "queued", "error"):
                t["status"] = "queued"; t["tries"] = 0; t["next_try"] = 0; ids.append(tid)
        save_lib(LIB)
    for tid in ids: queue_download(tid)
for _ in range(DL_WORKERS): threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=retry_scheduler, daemon=True).start()
recover_on_boot()

# ------------------------------------------------------------------ Spotify
def _extract_playlist_id(url):
    url = url.strip(); m = re.search(r"playlist[/:]([0-9A-Za-z]+)", url)
    if m: return m.group(1).split("?")[0]
    m = re.search(r"\b([0-9A-Za-z]{22})\b", url); return m.group(1) if m else None
def _img_first(images):
    try:
        if images: return images[0].url
    except Exception: pass
    return ""
def _dedup_tracks(tracks):
    seen, out = set(), []
    for t in tracks:
        k = make_key(t["title"], t["artist"])
        if k in seen: continue
        seen.add(k); out.append(t)
    return out
def _fetch_via_spotifyscraper(url, pid):
    try: from spotify_scraper import SpotifyClient
    except ImportError: raise RuntimeError("spotifyscraper introuvable")
    pl, last = None, None
    with SpotifyClient() as client:
        for target in (url, pid):
            try:
                pl = client.get_playlist(target, max_tracks=5000)
                if pl is not None: break
            except Exception as e: last = e
    if pl is None: raise RuntimeError(f"get_playlist a echoue ({last})")
    name = getattr(pl, "name", None) or "Playlist Spotify"; cover = _img_first(getattr(pl, "images", ())); tracks = []
    for pt in getattr(pl, "tracks", ()) or ():
        t = getattr(pt, "track", pt)
        if t is None: continue
        title = getattr(t, "name", "") or ""; artist = ", ".join(a.name for a in (getattr(t, "artists", ()) or ()) if getattr(a, "name", ""))
        album_obj = getattr(t, "album", None); album_name = getattr(album_obj, "name", "") if album_obj else ""
        cov = _img_first(getattr(t, "images", ()))
        if not cov and album_obj: cov = _img_first(getattr(album_obj, "images", ()))
        if not cov: cov = cover
        if title: tracks.append({"title": title, "artist": artist, "album": album_name, "cover": cov})
    if not tracks: raise RuntimeError("aucune piste extraite")
    return {"name": name, "cover": cover, "tracks": _dedup_tracks(tracks)}
def _extract_json(s, start):
    depth, instr, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if instr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
            continue
        if c == '"': instr = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(s[start:i + 1])
                except Exception: return None
    return None
def _is_track_dict(d):
    if not isinstance(d, dict): return False
    typ = d.get("type")
    if typ in ("album", "artist", "playlist", "show", "episode", "collection"): return False
    if typ == "track": return True
    if "name" not in d: return False
    if "duration_ms" in d or "duration" in d: return "artists" in d or "byArtist" in d
    return False
def _find_tracks(node, acc=None, depth=0):
    if acc is None: acc = []
    if depth > 12: return acc
    if isinstance(node, dict):
        if _is_track_dict(node): acc.append(node); return acc
        for v in node.values(): _find_tracks(v, acc, depth + 1)
    elif isinstance(node, list):
        for v in node: _find_tracks(v, acc, depth + 1)
    return acc
def _norm_track(d):
    if not isinstance(d, dict): return None
    title = d.get("name") or d.get("title") or ""; artists = d.get("artists") or d.get("byArtist") or []
    if isinstance(artists, list): artist = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
    elif isinstance(artists, dict): artist = artists.get("name", "")
    else: artist = str(artists) if artists else ""
    album = d.get("album") or {}; album_name = album.get("name", "") if isinstance(album, dict) else ""; cover = ""
    if isinstance(album, dict):
        imgs = album.get("images") or []
        if isinstance(imgs, list) and imgs:
            f = imgs[0]; cover = f.get("url", "") if isinstance(f, dict) else (f if isinstance(f, str) else "")
    if not cover:
        cover = d.get("cover_url") or d.get("cover") or d.get("image") or ""
        if isinstance(cover, list) and cover:
            f = cover[0]; cover = f.get("url", "") if isinstance(f, dict) else (f if isinstance(f, str) else "")
    return {"title": title, "artist": artist, "album": album_name, "cover": cover if isinstance(cover, str) else ""}
def _parse_jsonld(obj):
    name = obj.get("name", "Playlist Spotify"); tracks = []; tl = obj.get("track") or obj.get("tracks") or []
    if isinstance(tl, dict): tl = [tl]
    for t in tl:
        if not isinstance(t, dict): continue
        title = t.get("name", ""); by = t.get("byArtist") or t.get("artists") or []
        if isinstance(by, list): by = by[0] if by else {}
        artist = by.get("name", "") if isinstance(by, dict) else str(by)
        if title: tracks.append({"title": title, "artist": artist, "album": "", "cover": ""})
    if not tracks:
        for td in _find_tracks(obj):
            n = _norm_track(td)
            if n and n["title"]: tracks.append(n)
    return name, tracks
def _fetch_via_embed(pid):
    headers = {"User-Agent": UA, "Accept-Language": "en", "Accept": "text/html,application/xhtml+xml"}
    r = requests.get(f"https://open.spotify.com/embed/playlist/{pid}", headers=headers, timeout=25)
    if r.status_code != 200: raise RuntimeError(f"embed HTTP {r.status_code}")
    html = r.text
    for m in re.finditer(r'\{"@context"', html):
        obj = _extract_json(html, m.start())
        if obj:
            name, tracks = _parse_jsonld(obj)
            if tracks: return {"name": name, "cover": "", "tracks": _dedup_tracks(tracks)}
    idx = html.find('"resource":"')
    if idx != -1:
        try:
            start = idx + len('"resource":"'); raw = html[start:html.find('"', start)]; obj = None
            try: obj = json.loads(unquote(raw))
            except Exception:
                try: obj = json.loads(raw)
                except Exception: obj = None
            if obj:
                tracks = [n for n in (_norm_track(t) for t in _find_tracks(obj)) if n and n["title"]]
                if tracks:
                    name = obj.get("name", "Playlist Spotify") if isinstance(obj, dict) else "Playlist Spotify"; cov = ""
                    if isinstance(obj, dict):
                        imgs = obj.get("images") or []
                        if imgs and isinstance(imgs[0], dict): cov = imgs[0].get("url", "")
                    return {"name": name, "cover": cov, "tracks": _dedup_tracks(tracks)}
        except Exception: pass
    raise RuntimeError("structure embed non reconnue")
def scrape_spotify(url):
    pid = _extract_playlist_id(url)
    if not pid: raise ValueError("lien de playlist invalide")
    errors = []
    try: return _fetch_via_spotifyscraper(url, pid)
    except Exception as e: errors.append(str(e))
    try: return _fetch_via_embed(pid)
    except Exception as e: errors.append(str(e))
    raise RuntimeError("lecture impossible (" + " / ".join(errors) + "). Verifie le lien, ou utilise le collage manuel.")

# ------------------------------------------------------------------ paroles
LYRICS_CACHE = {}
def parse_lrc(text):
    lines = []
    for line in (text or "").split("\n"):
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if m: lines.append({"time": int(m.group(1)) * 60 + float(m.group(2)), "text": m.group(3).strip()})
    return lines

# ------------------------------------------------------------------ recherche multi-source (iTunes + Deezer)
def _s_itunes(q):
    try:
        r = requests.get("https://itunes.apple.com/search", params={"term": q, "media": "music", "limit": 14}, timeout=12)
        out = []
        for it in r.json().get("results", []):
            out.append({"title": it.get("trackName", ""), "artist": it.get("artistName", ""), "album": it.get("collectionName", ""),
                        "cover": (it.get("artworkUrl100", "") or "").replace("100x100", "600x600"), "year": (it.get("releaseDate") or "")[:4]})
        return out
    except Exception: return []
def _s_deezer(q):
    try:
        r = requests.get("https://api.deezer.com/search", params={"q": q, "limit": 14}, timeout=12)
        out = []
        for it in r.json().get("data", []):
            al = it.get("album") or {}; ar = it.get("artist") or {}
            out.append({"title": it.get("title", ""), "artist": ar.get("name", ""), "album": al.get("title", ""),
                        "cover": al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium") or "", "year": ""})
        return out
    except Exception: return []
def _merge_search(q):
    with ThreadPoolExecutor(max_workers=2) as ex:
        lists = [f.result() for f in (ex.submit(_s_itunes, q), ex.submit(_s_deezer, q))]
    seen, out = set(), []
    for lst in lists:
        for it in lst:
            k = make_key(it.get("title", ""), it.get("artist", ""))
            if not it.get("title") or k in seen: continue
            seen.add(k); out.append(it)
            if len(out) >= 24: return out
    return out

# ------------------------------------------------------------------ UI injectee (centre de telechargement + bouton vider), i18n
RETRY_UI = """<style>
#melodify-dlpanel{position:fixed;left:18px;bottom:118px;z-index:65;width:350px;max-width:calc(100vw - 36px);background:linear-gradient(180deg,rgba(22,21,25,.94),rgba(13,12,15,.96));border:1px solid rgba(255,255,255,.14);border-radius:18px;box-shadow:0 26px 60px -28px rgba(0,0,0,.92),0 2px 0 rgba(255,255,255,.05) inset;backdrop-filter:blur(20px) saturate(1.2);-webkit-backdrop-filter:blur(20px) saturate(1.2);font-family:'Plus Jakarta Sans',system-ui,sans-serif;color:#f4f1ea;transform:translateY(14px) scale(.98);opacity:0;pointer-events:none;transition:transform .4s cubic-bezier(.34,1.56,.64,1),opacity .35s ease;overflow:hidden}
#melodify-dlpanel.on{transform:none;opacity:1;pointer-events:auto}
#melodify-dlpanel::before{content:"";position:absolute;top:0;left:8%;right:8%;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.28),transparent)}
.mr-head{display:flex;align-items:center;gap:11px;padding:13px 15px;border-bottom:1px solid rgba(255,255,255,.08)}
.mr-ring{width:15px;height:15px;border-radius:50%;border:2px solid rgba(255,255,255,.18);border-top-color:var(--accent,#f4f1ea);flex:none;opacity:0;transition:opacity .3s}
.mr-ring.on{opacity:1;animation:mrsp .8s linear infinite}
@keyframes mrsp{to{transform:rotate(360deg)}}
.mr-hcount{display:flex;align-items:baseline;gap:7px}
.mr-count{font-family:'Fraunces',Georgia,serif;font-style:italic;font-weight:600;font-size:23px;line-height:1;letter-spacing:-.02em}
.mr-countlab{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:#6a655d}
.mr-spacer{flex:1}
.mr-failchip{display:none;font-family:'Space Mono',monospace;font-size:10px;letter-spacing:.06em;color:#ff9b9b;background:rgba(255,138,138,.12);border:1px solid rgba(255,138,138,.28);padding:4px 9px;border-radius:999px}
.mr-cancelall{display:none;font-family:'Space Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#f4f1ea;background:transparent;border:1px solid rgba(255,255,255,.22);padding:6px 11px;border-radius:999px;cursor:pointer;transition:.2s}
.mr-cancelall:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.4)}
.mr-body{max-height:300px;overflow:auto;padding:6px 8px 10px}
.mr-glabel{display:none;font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:#6a655d;padding:10px 8px 5px}
.mr-glabel.is-retry{color:#9a958c}.mr-glabel.is-todo{color:#ff9b9b}
.mr-grp{display:none}
.mr-row{display:grid;grid-template-columns:36px 1fr auto;align-items:center;gap:10px;padding:7px 8px;border-radius:11px;transition:background .2s;animation:mrin .32s cubic-bezier(.22,.61,.36,1) both}
.mr-row:hover{background:rgba(255,255,255,.05)}
.mr-row.mr-out{animation:mrout .24s ease forwards}
@keyframes mrin{from{opacity:0;transform:translateX(-7px)}to{opacity:1;transform:none}}
@keyframes mrout{to{opacity:0;transform:translateX(7px)}}
.mr-cov{width:36px;height:36px;border-radius:8px;object-fit:cover;background:#1c1b1f;box-shadow:0 4px 12px -6px rgba(0,0,0,.7)}
.mr-mid{min-width:0}
.mr-t{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.01em}
.mr-a{font-size:11px;color:#9a958c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.mr-bar{height:3px;border-radius:3px;background:rgba(255,255,255,.12);margin-top:6px;overflow:hidden}
.mr-fill{height:100%;width:0;background:var(--accent,#f4f1ea);border-radius:3px;transition:width .5s ease}
.mr-tag{display:inline-block;font-family:'Space Mono',monospace;font-size:8.5px;letter-spacing:.12em;text-transform:uppercase;margin-top:5px;padding:2px 7px;border-radius:999px}
.mr-tag.is-fail{color:#ff9b9b;background:rgba(255,138,138,.12)}.mr-tag.is-cancel{color:#9a958c;background:rgba(255,255,255,.07)}
.mr-err{font-size:10px;color:#6a655d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}
.mr-rt{font-family:'Space Mono',monospace;font-size:10px;color:#9a958c;margin-top:5px}
.mr-act{display:flex;gap:3px;align-self:center}
.mr-btn{width:27px;height:27px;border-radius:8px;display:grid;place-items:center;color:#9a958c;background:transparent;border:0;cursor:pointer;font-size:13px;line-height:1;opacity:.65;transition:transform .15s cubic-bezier(.34,1.56,.64,1),background .2s,color .2s,opacity .2s}
.mr-row:hover .mr-btn{opacity:1}
.mr-btn:hover{transform:scale(1.12);background:rgba(255,255,255,.08);color:#f4f1ea}
.mr-btn.mr-b-rm:hover{color:#ff9b9b;background:rgba(255,138,138,.12)}
.mr-body::-webkit-scrollbar{width:8px}.mr-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
</style>
<div id="melodify-dlpanel">
  <div class="mr-head"><span class="mr-ring"></span>
    <div class="mr-hcount"><span class="mr-count">0</span><span class="mr-countlab" data-i18n="dl.idle_lab">INACTIF</span></div>
    <span class="mr-spacer"></span><span class="mr-failchip">0</span>
    <button id="mr-cancelall" class="mr-cancelall" data-i18n="dl.cancelall">Tout annuler</button></div>
  <div class="mr-body">
    <div class="mr-glabel" data-i18n="dl.g_active">En cours</div><div data-grp="active" class="mr-grp"></div>
    <div class="mr-glabel is-retry" data-i18n="dl.g_retry">Nouvelles tentatives</div><div data-grp="retry" class="mr-grp"></div>
    <div class="mr-glabel is-todo" data-i18n="dl.g_todo">A relancer</div><div data-grp="todo" class="mr-grp"></div>
  </div>
</div>
<script>
(function(){
 var P=document.getElementById('melodify-dlpanel'); if(!P)return;
 var grp={active:P.querySelector('[data-grp=active]'),retry:P.querySelector('[data-grp=retry]'),todo:P.querySelector('[data-grp=todo]')};
 var ring=P.querySelector('.mr-ring'), cAll=P.querySelector('#mr-cancelall');
 var T=(window.t||function(k){return k;});
 function el(tag,cls,html){var e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e;}
 function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
 function post(u,d){fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).catch(function(){});}
 function btn(sym,cls,title,fn){var b=el('button','mr-btn '+cls,sym); b.title=title; b.onclick=function(e){e.stopPropagation(); fn();}; return b;}
 function rowFor(it,kind){
   var r=el('div','mr-row'); r.setAttribute('data-id',it.id);
   var cov=el('img','mr-cov'); cov.src='/api/cover/'+it.id; cov.loading='lazy';
   var mid=el('div','mr-mid'); mid.appendChild(el('div','mr-t',esc(it.title))); mid.appendChild(el('div','mr-a',esc(it.artist||'—')));
   if(kind==='active'){ var bar=el('div','mr-bar'); var fill=el('div','mr-fill'); bar.appendChild(fill); mid.appendChild(bar); r._fill=fill; }
   if(kind==='retry'){ mid.appendChild(el('div','mr-rt',T('dl.retry_word'))); }
   if(kind==='todo'){ mid.appendChild(el('span','mr-tag '+(it.status==='cancelled'?'is-cancel':'is-fail'), it.status==='cancelled'?T('dl.cancelled_word'):T('dl.fail_word')));
     if(it.error){ var er=el('div','mr-err',esc(it.error)); er.title=it.error; mid.appendChild(er); } }
   var act=el('div','mr-act');
   if(kind==='active'){ act.appendChild(btn('✕','mr-b-rm',T('dl.cancel_one'),function(){post('/api/cancel',{ids:[it.id]});})); }
   else { act.appendChild(btn('↻','mr-b-re',T('dl.retry_one'),function(){post('/api/retry',{ids:[it.id]});})); act.appendChild(btn('✕','mr-b-rm',T('dl.remove_one'),function(){post('/api/remove/'+it.id,{});})); }
   r.appendChild(cov); r.appendChild(mid); r.appendChild(act); return r;
 }
 function cssesc(s){return String(s).replace(/"/g,'\\\"');}
 function syncGroup(c,items,kind){ var seen={};
   items.forEach(function(it,i){ seen[it.id]=1; var r=c.querySelector('.mr-row[data-id="'+cssesc(it.id)+'"]');
     if(!r){ r=rowFor(it,kind); r.style.animationDelay=(Math.min(i,8)*25)+'ms'; c.appendChild(r); }
     else if(kind==='active'&&r._fill){ r._fill.style.width=(it.percent||0)+'%'; } });
   Array.prototype.forEach.call(c.querySelectorAll('.mr-row'),function(r){ if(!seen[r.getAttribute('data-id')]){ r.classList.add('mr-out'); setTimeout(function(){ if(r.parentNode)r.parentNode.removeChild(r); },240); } });
   c.style.display=items.length?'':'none'; var lab=c.previousElementSibling; if(lab&&lab.classList.contains('mr-glabel')) lab.style.display=items.length?'':'none'; }
 function tick(){ if(document.hidden)return;
   fetch('/api/queue').then(function(r){return r.json();}).then(function(q){
     var a=q.active||[], rt=q.retrying||[], f=q.failed||[]; var busy=a.length+rt.length;
     ring.classList.toggle('on',busy>0);
     P.querySelector('.mr-count').textContent=busy>0?busy:(f.length?'!':'0');
     P.querySelector('.mr-countlab').textContent=busy>0?T('dl.active_lab'):(f.length?T('dl.todo_lab'):T('dl.idle_lab'));
     var fc=P.querySelector('.mr-failchip'); fc.style.display=f.length?'':'none'; fc.textContent=f.length? (f.length+' '+T('dl.fail_word')):'';
     cAll.style.display=busy>0?'':'none';
     syncGroup(grp.active,a,'active'); syncGroup(grp.retry,rt,'retry'); syncGroup(grp.todo,f,'todo');
     P.classList.toggle('on', busy>0 || f.length>0);
   }).catch(function(){}); }
 if(cAll)cAll.onclick=function(){post('/api/cancel',{all:true});};
 tick(); setInterval(tick,1200);
})();
</script>"""
KILL_UI = """<style>
#melodify-kill{position:fixed;right:18px;bottom:118px;z-index:66;display:none;align-items:center;gap:10px;padding:11px 16px;border-radius:999px;background:linear-gradient(180deg,rgba(40,16,18,.92),rgba(24,10,12,.95));border:1px solid rgba(255,120,120,.34);color:#ffd9d9;font:700 11px/1 'Space Mono',monospace;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 18px 40px -18px rgba(255,60,60,.5),0 1px 0 rgba(255,255,255,.06) inset;transition:transform .25s cubic-bezier(.34,1.56,.64,1),box-shadow .25s,background .2s;animation:mkIn .4s cubic-bezier(.34,1.56,.64,1) both}
#melodify-kill:hover{transform:translateY(-2px);box-shadow:0 22px 48px -16px rgba(255,60,60,.62)}
#melodify-kill:active{transform:translateY(0) scale(.97)}
#melodify-kill[disabled]{opacity:.5;cursor:wait}
#melodify-kill svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
#melodify-kill .mk-n{min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:rgba(255,120,120,.22);display:grid;place-items:center;font-size:10px}
#melodify-kill.confirm{background:linear-gradient(180deg,#ff5a5a,#e23b3b);color:#1a0606;border-color:transparent}
@keyframes mkIn{from{opacity:0;transform:translateY(12px) scale(.9)}to{opacity:1;transform:none}}
#melodify-kill.gone{animation:mkOut .3s ease forwards}
@keyframes mkOut{to{opacity:0;transform:translateY(10px) scale(.9)}}
</style>
<script>
(function(){
 var b=document.createElement('button'); b.id='melodify-kill';
 b.innerHTML='<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg><span data-i18n="kill.label">Vider la file</span><span class="mk-n">0</span>';
 document.body.appendChild(b);
 var T=(window.t||function(k){return k;});
 var label=b.querySelector('span'); var chip=b.querySelector('.mk-n'); var armed=false;
 function poll(){ if(document.hidden)return; fetch('/api/clear-status').then(function(r){return r.json();}).then(function(d){
   var n=d.n||0; chip.textContent=n;
   if(n>0){ b.style.display='flex'; b.classList.remove('gone'); }
   else { b.classList.add('gone'); setTimeout(function(){ if((parseInt(chip.textContent)||0)===0 && !armed) b.style.display='none'; },300); }
 }).catch(function(){}); if(!armed) label.textContent=T('kill.label'); }
 b.onclick=function(){ if(b.disabled) return;
   if(!armed){ armed=true; b.classList.add('confirm'); label.textContent=T('kill.confirm'); setTimeout(function(){ if(armed){ armed=false; b.classList.remove('confirm'); label.textContent=T('kill.label'); } },3000); return; }
   armed=false; b.classList.remove('confirm'); label.textContent=T('kill.label'); b.disabled=true;
   fetch('/api/clear-all',{method:'POST'}).then(function(){ setTimeout(function(){ try{ if(typeof load==='function') load(); }catch(e){} b.disabled=false; poll(); },250); }).catch(function(){ b.disabled=false; }); };
 poll(); setInterval(poll,1500);
})();
</script>"""

# ===================== Flask =====================
app = Flask(__name__); app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
@app.after_request
def _nocache(resp):
    if resp.mimetype and resp.mimetype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"; resp.headers["Pragma"] = "no-cache"; resp.headers["Expires"] = "0"
    return resp
@app.route("/")
def index():
    html = (BASE / "index.html").read_text(encoding="utf-8")
    if "melodify-dlpanel" not in html: html = html.replace("</body>", RETRY_UI + "\n</body>", 1)
    if "melodify-kill" not in html: html = html.replace("</body>", KILL_UI + "\n</body>", 1)
    return Response(html, mimetype="text/html")
@app.route("/favicon.ico")
def favicon(): return Response(status=204)
@app.route("/api/prefs", methods=["GET", "POST"])
def api_prefs():
    if request.method == "POST":
        d = request.json or {}
        if isinstance(d, dict): save_prefs_file(d)
        return jsonify({"ok": True})
    return jsonify(load_prefs())
@app.route("/api/last", methods=["GET", "POST"])
def api_last():
    if request.method == "POST":
        d = request.json or {}
        if isinstance(d, dict) and d.get("id"): _save_last({"id": d.get("id"), "t": float(d.get("t") or 0), "pl": d.get("pl") or "", "idx": int(d.get("idx") or 0)})
        return jsonify({"ok": True})
    return jsonify(_load_last())
@app.route("/api/disk")
def api_disk():
    b = 0; cov = 0
    for f in CACHE.glob("*.*"):
        if f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg"):
            try: b += f.stat().st_size
            except Exception: pass
    for f in COVERS.glob("*.jpg"):
        try: b += f.stat().st_size; cov += 1
        except Exception: pass
    with lock: tr = sum(1 for t in LIB["tracks"].values() if t.get("status") == "ready")
    return jsonify({"bytes": b, "tracks": tr, "covers": cov})

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    url = (request.json or {}).get("url", "").strip()
    if not url: return jsonify({"error": "no url"}), 400
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        return jsonify({"title": (info.get("title") or "").strip(), "artist": (info.get("artist") or info.get("uploader") or info.get("channel") or "").strip(), "cover": (info.get("thumbnail") or "").strip(), "duration": int(info.get("duration") or 0), "url": url})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400


@app.route("/api/library")
def api_library():
    with lock:
        tracks = sorted(LIB["tracks"].values(), key=lambda t: t.get("added", 0), reverse=True)
        return jsonify({"tracks": tracks, "playlists": list(LIB["playlists"].values())})
@app.route("/api/queue")
def api_queue():
    with dl_lock:
        dl = sum(1 for j in jobs.values() if j["status"] == "downloading"); qd = sum(1 for j in jobs.values() if j["status"] == "queued")
    with lock:
        snap = list(LIB["tracks"].values())
    retrying = sum(1 for t in snap if t.get("status") == "error" and t.get("tries", 0) < MAX_TRIES)
    failed = [{"id": t["id"], "title": t.get("title", ""), "artist": t.get("artist", ""), "error": t.get("last_error", ""), "status": t.get("status")}
              for t in snap if t.get("status") in ("error", "cancelled") and (t.get("status") != "error" or t.get("tries", 0) >= MAX_TRIES or t.get("status") == "cancelled")]
    failed = [t for t in snap if t.get("status") == "cancelled" or (t.get("status") == "error" and t.get("tries", 0) >= MAX_TRIES)]
    failed = [{"id": t["id"], "title": t.get("title", ""), "artist": t.get("artist", ""), "error": t.get("last_error", ""), "status": t.get("status")} for t in failed]
    return jsonify({"active": [{"id": t["id"], "title": t.get("title", ""), "artist": t.get("artist", ""), "percent": (jobs.get(t["id"], {}).get("percent", 0) if t.get("status") == "downloading" else 0), "status": t.get("status")} for t in snap if t.get("status") in ("downloading", "queued")],
                    "retrying": [{"id": t["id"], "title": t.get("title", ""), "artist": t.get("artist", ""), "error": t.get("last_error", ""), "status": "error"} for t in snap if t.get("status") == "error" and t.get("tries", 0) < MAX_TRIES],
                    "failed": failed, "pending": dl + qd + retrying})
@app.route("/api/clear-all", methods=["POST"])
def api_clear_all():
    removed = []
    with lock:
        for tid in list(LIB["tracks"].keys()):
            if LIB["tracks"][tid].get("status") != "ready": LIB["tracks"].pop(tid, None); removed.append(tid)
        for tid in removed:
            for pl in LIB["playlists"].values(): pl["tracks"] = [x for x in pl["tracks"] if x != tid]
        save_lib(LIB)
    with dl_lock: dl_queue.clear(); jobs.clear()
    for tid in removed:
        for f in CACHE.glob(tid + ".*"): f.unlink(missing_ok=True)
        (COVERS / (tid + ".jpg")).unlink(missing_ok=True)
    return jsonify({"ok": True, "cleared": len(removed)})
@app.route("/api/clear-status")
def api_clear_status():
    with lock: n = sum(1 for t in LIB["tracks"].values() if t.get("status") != "ready")
    with dl_lock: n += len(dl_queue)
    return jsonify({"n": n})
@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    d = request.json or {}; do_all = bool(d.get("all")); ids = d.get("ids") or []
    with lock:
        if do_all: ids = [tid for tid, t in LIB["tracks"].items() if t.get("status") in ("queued", "downloading") or (t.get("status") == "error" and t.get("tries", 0) < MAX_TRIES)]
    for tid in ids: _cancel_one(tid)
    return jsonify({"ok": True, "cancelled": len(ids)})
@app.route("/api/retry", methods=["POST"])
def api_retry():
    d = request.json or {}; ids = d.get("ids")
    with lock:
        targets = [tid for tid in (ids or LIB["tracks"].keys()) if LIB["tracks"].get(tid, {}).get("status") in ("error", "cancelled")] if ids else [tid for tid, t in LIB["tracks"].items() if t.get("status") in ("error", "cancelled")]
        for tid in targets:
            t = LIB["tracks"][tid]; t["status"] = "downloading"; t["tries"] = 0; t["next_try"] = 0; t["last_error"] = ""
        if targets: save_lib(LIB)
    for tid in targets: queue_download(tid)
    return jsonify({"ok": True, "retried": len(targets)})
@app.route("/api/cover/<tid>")
def api_cover(tid):
    local = COVERS / f"{tid}.jpg"
    if local.exists(): return send_file(local, mimetype="image/jpeg")
    t = LIB["tracks"].get(tid, {}); url = t.get("cover")
    if url:
        fetch_cover(tid, url)
        if local.exists(): return send_file(local, mimetype="image/jpeg")
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200: return Response(r.content, mimetype="image/jpeg")
        except Exception: pass
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="100%" height="100%" fill="#1c1b1f"/><text x="50%" y="50%" fill="#5a574f" font-family="sans-serif" font-size="64" text-anchor="middle" dy=".3em">♪</text></svg>'
    return Response(svg, mimetype="image/svg+xml")
@app.route("/api/stream/<tid>")
def api_stream(tid):
    p = file_for(tid)
    if not p: abort(404)
    size = p.stat().st_size
    _MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm", ".opus": "audio/ogg", ".ogg": "audio/ogg"}
    mime = _MIME.get(p.suffix.lower(), "audio/mpeg"); rng = request.headers.get("Range")
    if rng:
        m = re.search(r"bytes=(\d+)-(\d*)", rng); start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1; end = min(end, size - 1); length = end - start + 1
        def gen():
            with open(p, "rb") as f:
                f.seek(start); left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk: break
                    left -= len(chunk); yield chunk
        r = Response(gen(), 206, mimetype=mime, direct_passthrough=True); r.headers["Content-Range"] = f"bytes {start}-{end}/{size}"; r.headers["Content-Length"] = str(length)
    else:
        def gen():
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    yield chunk
        r = Response(gen(), 200, mimetype=mime, direct_passthrough=True); r.headers["Content-Length"] = str(size)
    r.headers["Accept-Ranges"] = "bytes"; return r
@app.route("/api/search", methods=["POST"])
def api_search():
    q = (request.json or {}).get("q", "").strip()
    if not q: return jsonify([])
    return jsonify(_merge_search(q))
@app.route("/api/add", methods=["POST"])
def api_add():
    d = request.json or {}; tid, existed = create_track(d.get("title", "Sans titre"), d.get("artist", "Inconnu"), d.get("album", ""), d.get("cover", ""), d.get("query"))
    if not existed: queue_download(tid)
    return jsonify({"id": tid, "existed": existed})
@app.route("/api/spotify", methods=["POST"])
def api_spotify():
    url = (request.json or {}).get("url", "").strip()
    try: return jsonify(scrape_spotify(url))
    except Exception as e: return jsonify({"error": str(e)}), 400
@app.route("/api/spotify/import", methods=["POST"])
def api_spotify_import():
    d = request.json or {}; tracks = d.get("tracks", []); name = d.get("name", "Playlist importee"); cover = d.get("cover", ""); ids, added = [], 0
    for t in tracks:
        q = t.get("query") or f"{t.get('artist','')} {t.get('title','')}".strip(); tid, existed = create_track(t.get("title", "?"), t.get("artist", "?"), t.get("album", ""), t.get("cover", cover), q)
        if not existed: queue_download(tid); added += 1
        ids.append(tid)
    pid = new_id()
    with lock:
        LIB["playlists"][pid] = {"id": pid, "name": name, "cover": cover, "tracks": list(dict.fromkeys(ids)), "added": int(time.time())}; save_lib(LIB)
    return jsonify({"playlist": pid, "ids": ids, "added": added})
@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f: abort(400)
    tid = new_id(); dest = CACHE / f"{tid}.mp3"; f.save(dest); title, artist, album = dest.stem, "Fichier local", ""
    try:
        from mutagen import File; m = File(str(dest))
        if m and m.tags:
            title = str(m.tags.get("TIT2", title)); artist = str(m.tags.get("TPE1", artist)); album = str(m.tags.get("TALB", album))
            apic = m.tags.get("APIC:Cover") or m.tags.get("APIC")
            if apic: (COVERS / f"{tid}.jpg").write_bytes(apic.data)
    except Exception: pass
    ex = find_existing(title, artist)
    if ex: dest.unlink(missing_ok=True); return jsonify({"id": ex, "existed": True})
    with lock:
        LIB["tracks"][tid] = {"id": tid, "title": title, "artist": artist, "album": album, "cover": "", "duration": duration_of(dest), "file": dest.name, "status": "ready", "query": "", "added": int(time.time()), "fav": False, "tries": 0, "next_try": 0, "last_error": ""}; save_lib(LIB)
    return jsonify({"id": tid, "existed": False})
@app.route("/api/remove/<tid>", methods=["POST"])
def api_remove(tid):
    p = file_for(tid)
    if p: p.unlink(missing_ok=True)
    (COVERS / f"{tid}.jpg").unlink(missing_ok=True)
    with lock:
        LIB["tracks"].pop(tid, None)
        for pl in LIB["playlists"].values(): pl["tracks"] = [x for x in pl["tracks"] if x != tid]
        save_lib(LIB)
    return jsonify({"ok": True})
@app.route("/api/remove-batch", methods=["POST"])
def api_remove_batch():
    ids = (request.json or {}).get("ids", []); removed = 0
    with lock:
        for tid in ids:
            if tid not in LIB["tracks"]: continue
            LIB["tracks"].pop(tid, None)
            for pl in LIB["playlists"].values(): pl["tracks"] = [x for x in pl["tracks"] if x != tid]
            removed += 1
        save_lib(LIB)
    for tid in ids:
        for f in CACHE.glob(f"{tid}.*"):
            if f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus", ".ogg"): f.unlink(missing_ok=True)
        (COVERS / f"{tid}.jpg").unlink(missing_ok=True)
    return jsonify({"ok": True, "removed": removed})
@app.route("/api/fav/<tid>", methods=["POST"])
def api_fav(tid):
    with lock:
        if tid in LIB["tracks"]: LIB["tracks"][tid]["fav"] = not LIB["tracks"][tid].get("fav", False); save_lib(LIB); return jsonify({"fav": LIB["tracks"][tid]["fav"]})
    abort(404)
@app.route("/api/playlists", methods=["POST"])
def api_playlist_create():
    d = request.json or {}; pid = new_id()
    with lock:
        LIB["playlists"][pid] = {"id": pid, "name": d.get("name", "Nouvelle playlist"), "cover": d.get("cover", ""), "tracks": [], "added": int(time.time())}; save_lib(LIB)
    return jsonify({"id": pid})
@app.route("/api/playlists/<pid>/rename", methods=["POST"])
def api_playlist_rename(pid):
    name = (request.json or {}).get("name", "").strip()
    if not name: abort(400)
    with lock:
        if pid in LIB["playlists"]: LIB["playlists"][pid]["name"] = name; save_lib(LIB); return jsonify({"ok": True})
    abort(404)
@app.route("/api/playlists/<pid>/cover", methods=["POST"])
def api_playlist_cover(pid):
    f = request.files.get("file")
    if not f: abort(400)
    dest = COVERS / f"pl_{pid}.jpg"; f.save(dest)
    with lock:
        if pid in LIB["playlists"]: LIB["playlists"][pid]["cover"] = f"/api/plcover/{pid}"; save_lib(LIB)
    return jsonify({"cover": f"/api/plcover/{pid}"})
@app.route("/api/plcover/<pid>")
def api_plcover(pid):
    p = COVERS / f"pl_{pid}.jpg"
    if p.exists(): return send_file(p, mimetype="image/jpeg")
    abort(404)
@app.route("/api/playlists/<pid>", methods=["DELETE"])
def api_playlist_delete(pid):
    with lock: LIB["playlists"].pop(pid, None); save_lib(LIB)
    (COVERS / f"pl_{pid}.jpg").unlink(missing_ok=True); return jsonify({"ok": True})
@app.route("/api/playlists/<pid>/add", methods=["POST"])
def api_playlist_add(pid):
    tids = (request.json or {}).get("ids", [])
    with lock:
        pl = LIB["playlists"].get(pid)
        if not pl: abort(404)
        for tid in tids:
            if tid in pl["tracks"]: continue
            if tid in LIB["tracks"]: pl["tracks"].append(tid)
        save_lib(LIB)
    return jsonify({"ok": True, "count": len(pl["tracks"])})
@app.route("/api/playlists/<pid>/remove", methods=["POST"])
def api_playlist_remove(pid):
    tid = (request.json or {}).get("id")
    with lock:
        pl = LIB["playlists"].get(pid)
        if not pl: abort(404)
        pl["tracks"] = [x for x in pl["tracks"] if x != tid]; save_lib(LIB)
    return jsonify({"ok": True})
@app.route("/api/lyrics")
def api_lyrics():
    title = request.args.get("title", ""); artist = request.args.get("artist", ""); album = request.args.get("album", ""); duration = request.args.get("duration", "")
    key = (title.lower(), artist.lower())
    if key in LYRICS_CACHE: return jsonify(LYRICS_CACHE[key])
    headers = {"User-Agent": "Melodify/1.0 (local desktop music player)"}; result = {"plain": "", "synced": [], "instrumental": False}
    try:
        params = {"track_name": title, "artist_name": artist}
        if album: params["album_name"] = album
        if duration and duration != "0": params["duration"] = duration
        r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            r = requests.get("https://lrclib.net/api/search", params={"track_name": title, "artist_name": artist}, headers=headers, timeout=15)
            items = r.json() if r.status_code == 200 else []; data = items[0] if items else {}
        else: data = r.json()
        result = {"plain": data.get("plainLyrics") or "", "synced": parse_lrc(data.get("syncedLyrics") or ""), "instrumental": bool(data.get("instrumental"))}
    except Exception as e: result = {"plain": "", "synced": [], "instrumental": False, "error": str(e)}
    LYRICS_CACHE[key] = result; return jsonify(result)

if __name__ == "__main__":
    print(f"\n  Melodify (mode navigateur) sur  http://localhost:{PORT}\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)