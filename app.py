#!/usr/bin/env python3
# Melodify - fenetre native + serveur + tray + Discord RPC + DUCKING.
# Vitre = transparent=True. Tray + crochet micro demarres APRES webview.start
# (callback) -> pas de course GTK. Crochet micro ONE-SHOT (permission-request ->
# allow, s'arrete des que branche, pas de polling). Moniteur PipeWire de la
# SORTIE audio dans un thread daemon defensif -> pilote window.__sysDuckActive
# via evaluate_js ; voyant d'etat pousse au JS. Tout en try/except.
import os, sys, time, socket, struct, json, uuid, queue, math, threading
from pathlib import Path

PORT = 7777
BASE = Path(__file__).resolve().parent
WIN = None
_sysduck_on = threading.Event()

def _port_busy(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", p)); return False
        except OSError:
            return True

def _run_server():
    sys.path.insert(0, str(BASE))
    import server
    server.app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False)

class DiscordRPC:
    def __init__(self):
        self._q = queue.Queue(); self.sock = None; self.client_id = None; self._alive = False
        self._pending_act = None; self._dirty = False; self._cfg = None; self._backoff = 0.0
        threading.Thread(target=self._run, daemon=True).start()
    def configure(self, c): self._q.put(('cfg', str(c or '').strip()))
    def set(self, a): self._q.put(('set', a))
    def clear(self): self._q.put(('clear',))
    def _paths(self):
        out, seen = [], set()
        for b in [os.environ.get('XDG_RUNTIME_DIR'), os.environ.get('TMPDIR')]:
            if b:
                for i in range(10):
                    p = os.path.join(b, 'discord-ipc-%d' % i)
                    if p not in seen: seen.add(p); out.append(p)
        try:
            base = '/run/user/%d' % os.getuid()
            for i in range(10):
                p = os.path.join(base, 'discord-ipc-%d' % i)
                if p not in seen: seen.add(p); out.append(p)
        except Exception: pass
        return out
    def _connect(self, cid):
        self._close_sock()
        for p in self._paths():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2.0); s.connect(p); self.sock = s; break
            except Exception: self.sock = None
        if not self.sock: return False
        try:
            self._send(0, {"v": 1, "client_id": cid}); self._recv(); self.sock.settimeout(0.4)
            self._alive = True; self.client_id = cid; print(">> discord rpc: connecte (client_id=%s)" % cid); return True
        except Exception as e:
            print(">> discord rpc: handshake echec:", e); self._close_sock(); return False
    def _send(self, op, payload):
        body = json.dumps(payload).encode('utf-8'); self.sock.sendall(struct.pack('<II', op, len(body)) + body)
    def _recv(self):
        hdr = self._recvall(8)
        if not hdr: return None
        op, ln = struct.unpack('<II', hdr); body = self._recvall(ln) if ln else b''
        if body is None: return None
        try: return (op, json.loads(body.decode('utf-8')))
        except Exception: return (op, None)
    def _recvall(self, n):
        buf = b''
        while len(buf) < n:
            try: chunk = self.sock.recv(n - len(buf))
            except Exception: return None
            if not chunk: return None
            buf += chunk
        return buf
    def _drain(self):
        for _ in range(8):
            try: m = self._recv()
            except Exception: self._alive = False; return
            if m is None: return
            if m[0] == 3:
                try: self._send(4, m[1] or {})
                except Exception: self._alive = False; return
    def _send_activity(self, act):
        try: self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": act}, "nonce": str(uuid.uuid4())})
        except Exception: self._alive = False
    def _close_sock(self):
        self._alive = False
        try:
            if self.sock: self.sock.close()
        except Exception: pass
        self.sock = None
    def _run(self):
        while True:
            while True:
                try: cmd = self._q.get_nowait()
                except queue.Empty: break
                if cmd[0] == 'cfg': self._cfg = cmd[1] or None; self._close_sock(); self._pending_act = None; self._dirty = False
                elif cmd[0] == 'set': self._pending_act = cmd[1]; self._dirty = True
                elif cmd[0] == 'clear': self._pending_act = None; self._dirty = True
            if self._cfg and not self._alive:
                if time.time() >= self._backoff:
                    if self._connect(self._cfg): self._backoff = 0.0; self._dirty = True
                    else: self._backoff = time.time() + 8.0
            elif not self._cfg and self._alive: self._close_sock()
            if self._alive:
                if self._dirty: self._send_activity(self._pending_act); self._dirty = False
                self._drain()
            else: time.sleep(0.2)
RPC = DiscordRPC()

def _duck_monitor_thread():
    import subprocess
    FIFO = '/tmp/melodify_duck_mon'
    mon = [None]; proc = [None]; fd = [-1]; pstat = [None]; pact = [None]; hold = [0]
    def ev(js):
        try:
            if WIN: WIN.evaluate_js(js)
        except Exception: pass
    def status(st):
        if pstat[0] == st: return
        pstat[0] = st; ev("window.__sysDuckStatus=%r;" % st)
    def active(a):
        if pact[0] == a: return
        pact[0] = a; ev("window.__sysDuckActive=%s;" % ('true' if a else 'false'))
    def find_monitor():
        try:
            out = subprocess.run(['pactl', 'list', 'sources', 'short'], capture_output=True, text=True, timeout=4).stdout
            for line in out.splitlines():
                parts = line.split('\t'); name = parts[1] if len(parts) > 1 else parts[0]
                if 'monitor' in name: return name
        except Exception: pass
        return None
    def stop_proc():
        try:
            if fd[0] >= 0: os.close(fd[0])
        except Exception: pass
        fd[0] = -1
        try:
            if proc[0]: proc[0].kill()
        except Exception: pass
        proc[0] = None
    def start_proc():
        if not mon[0]: mon[0] = find_monitor()
        if not mon[0]: status('missing'); return False
        try: os.mkfifo(FIFO)
        except FileExistsError: pass
        except Exception: status('missing'); return False
        try:
            proc[0] = subprocess.Popen(['parecord', '-d', mon[0], '--rate=16000', '--channels=1', '--format=s16le', FIFO],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: status('missing'); return False
        try:
            fd[0] = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
        except Exception: status('missing'); stop_proc(); return False
        status('ready'); return True
    while True:
        time.sleep(0.12)
        on = _sysduck_on.is_set()
        if on and proc[0] is None:
            if not start_proc(): time.sleep(2.0)
        if (not on) and proc[0] is not None:
            stop_proc(); active(False); status('off')
        if on and proc[0] is not None:
            if proc[0].poll() is not None:
                stop_proc(); status('missing'); continue
            try: chunk = os.read(fd[0], 3200)
            except Exception: chunk = b''
            if chunk and len(chunk) >= 2:
                n = len(chunk) // 2
                try: vals = struct.unpack('<%dh' % n, chunk[:n * 2])
                except Exception: vals = ()
                s = 0.0
                for v in vals:
                    x = v / 32768.0; s += x * x
                rms = math.sqrt(s / n) if n else 0.0
                if rms > 0.02: hold[0] = 4
                elif hold[0] > 0: hold[0] -= 1
                active(hold[0] > 0)

class Api:
    def configure_rpc(self, client_id):
        try: RPC.configure(client_id); return {'ok': True}
        except Exception as e: return {'ok': False, 'error': str(e)}
    def set_rpc(self, activity):
        try: RPC.set(activity); return {'ok': True}
        except Exception as e: return {'ok': False, 'error': str(e)}
    def clear_rpc(self):
        try: RPC.clear(); return {'ok': True}
        except Exception as e: return {'ok': False, 'error': str(e)}
    def set_sysduck(self, on):
        try:
            if on: _sysduck_on.set()
            else: _sysduck_on.clear()
            return {'ok': True}
        except Exception as e: return {'ok': False, 'error': str(e)}

def _walk(c):
    yield c
    try: k = c.get_children()
    except Exception: k = []
    for ch in k: yield from _walk(ch)

def _apply_perms():
    try:
        import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk, GLib
        WebKit2 = None
        for ver in ("4.1", "4.0"):
            try: gi.require_version('WebKit2', ver); from gi.repository import WebKit2 as _W; WebKit2 = _W; break
            except Exception: continue
        if WebKit2 is None:
            print(">> mic hook: WebKit2 introuvable"); return
        state = {'n': 0}
        def hook():
            state['n'] += 1
            for w in Gtk.Window.list_toplevels():
                for c in _walk(w):
                    if isinstance(c, WebKit2.WebView):
                        try:
                            c.connect('permission-request', lambda wv, req: (req.allow(), True))
                            print(">> mic permission auto-allow branche")
                        except Exception as e: print("mic connect:", e)
                        return False
            return state['n'] < 40
        GLib.idle_add(hook)
    except Exception as e:
        print(">> mic hook skip:", e)

def _lerp(a, b, t): return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))
def _make_icon_image():
    from PIL import Image, ImageDraw
    # MI-ICON
    _ip = BASE / 'icon.png'
    if _ip.exists():
        try: return Image.open(_ip).convert('RGBA').resize((64, 64), Image.LANCZOS)
        except Exception: pass
    S = 256; img = Image.new('RGBA', (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
    c = S // 2; R = 120; center = (255, 214, 162); edge = (150, 60, 22)
    for r in range(R, 0, -1):
        t = 1.0 - (r / R); col = _lerp(edge, center, t * t); d.ellipse([c - r, c - r, c + r, c + r], fill=col + (255,))
    gloss = Image.new('RGBA', (S, S), (0, 0, 0, 0)); gd = ImageDraw.Draw(gloss)
    gd.ellipse([c - 92, c - 104, c + 92, c - 6], fill=(255, 250, 240, 70)); img = Image.alpha_composite(img, gloss); d = ImageDraw.Draw(img)
    for r in (90, 62): d.ellipse([c - r, c - r, c + r, c + r], outline=(110, 44, 16, 90), width=2)
    d.ellipse([c - 38, c - 38, c + 38, c + 38], fill=(26, 17, 11, 255)); cream = (246, 239, 226, 255)
    d.ellipse([c - 30, c + 22, c + 4, c + 52], fill=cream); d.rounded_rectangle([c - 8, c - 64, c + 2, c + 30], radius=5, fill=cream)
    d.polygon([(c + 2, c - 64), (c + 40, c - 52), (c + 34, c - 18), (c + 2, c - 34)], fill=cream)
    return img.resize((64, 64), Image.LANCZOS)
def _tray_thread():
    try: import gi; gi.require_version('Gtk', '3.0')
    except Exception: pass
    try: import pystray; from PIL import Image, ImageDraw  # noqa
    except Exception: print(">> tray: pystray/Pillow absents ->  .venv/bin/pip install pystray pillow"); return
    try:
        img = _make_icon_image(); visible = [True]
        def show_hide(icon, item):
            try:
                if visible[0] and WIN: WIN.hide(); visible[0] = False
                elif WIN: WIN.show(); visible[0] = True
            except Exception as e: print("tray vis:", e)
        def js(code):
            try:
                if WIN: WIN.evaluate_js(code)
            except Exception as e: print("tray js:", e)
        def do_quit(icon, item):
            try: icon.stop()
            except Exception: pass
            try:
                if WIN: WIN.destroy()
            except Exception: pass
            os._exit(0)
        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide", show_hide, default=True),
            pystray.MenuItem("Play / Pause", lambda ic, it: js("try{toggle()}catch(e){}")),
            pystray.MenuItem("Suivant", lambda ic, it: js("try{next(false)}catch(e){}")),
            pystray.MenuItem("Precedent", lambda ic, it: js("try{prev()}catch(e){}")),
            pystray.Menu.SEPARATOR, pystray.MenuItem("Quitter", do_quit))
        icon = pystray.Icon("melodify", img, "Melodify", menu); print(">> tray: icone systeme active (clic droit = menu)"); icon.run()
    except Exception as e:
        print(">> tray indisponible:", e); print("   sous Arch/sway :  sudo pacman -S libayatana-appindicator  (ou AUR libappindicator-gtk3)")

def _boot():
    try:
        # MI-BOOT
        if IS_MASTER: threading.Thread(target=_tray_thread, daemon=True).start()
    except Exception as e: print("tray start failed:", e)
    _apply_perms()

def _make_window():
    import webview; global WIN
    common = dict(url=f"http://127.0.0.1:{PORT}", width=1440, height=900, min_size=(980, 640),
                  text_select=True, easy_drag=False, resizable=True, js_api=Api())
    try: WIN = webview.create_window("Melodify", transparent=True, **common)
    except TypeError: WIN = webview.create_window("Melodify", **common)
    return WIN

# MI-HELPERS
IS_MASTER = True
try: PORT = int(os.environ.get('MELODIFY_PORT', '7777'))
except Exception: PORT = 7777
def _lock_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), 'melodify-%d-%d.lock' % (os.getuid(), PORT))
def _read_lock(path):
    try:
        with open(path) as f: return int(f.read().strip())
    except Exception: return None
def _write_lock(path, pid):
    try:
        with open(path, 'w') as f: f.write(str(pid))
    except Exception: pass
def _pid_alive(pid):
    try: os.kill(pid, 0); return True
    except OSError: return False
def _ping(port):
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=0.8).close(); return True
    except urllib.error.HTTPError: return True
    except Exception: return False

# MI-MAIN
def main():
    global IS_MASTER
    lock = _lock_path(); mp = _read_lock(lock); up = _ping(PORT); IS_MASTER = True
    if up:
        IS_MASTER = False
    elif mp is not None and _pid_alive(mp):
        for _ in range(40):
            time.sleep(0.25)
            if _ping(PORT): IS_MASTER = False; break
    if IS_MASTER:
        _write_lock(lock, os.getpid())
        threading.Thread(target=_run_server, daemon=True).start()
        for _ in range(60):
            if _port_busy(PORT): break
            time.sleep(0.1)
        threading.Thread(target=_duck_monitor_thread, daemon=True).start()
    try: import webview
    except ImportError: print("pywebview absent -> pip install pywebview"); sys.exit(1)
    _make_window()
    try: webview.start(_boot, debug=False, gui='gtk')
    except Exception as e:
        print("gtk start failed, fallback:", e)
        try: webview.start(_boot, debug=False)
        except Exception as e2: print("start failed:", e2)
    if IS_MASTER:
        try:
            if _read_lock(lock) == os.getpid(): os.remove(lock)
        except Exception: pass

if __name__ == "__main__":
    main()
