#!/usr/bin/env bash
set -u   # PAS de -e : on veut "best effort" sur les paquets systeme
cd "$(dirname "$0")"
SUDO=""; if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null; then SUDO="sudo"; fi

install_sys() {
  if command -v pacman >/dev/null; then
    $SUDO pacman -S --needed --noconfirm python python-pip python-gobject gtk3 ffmpeg pipewire pulseaudio || true
    # nom Arch correct, avec fallback 4.0 ; un mauvais nom ne bloque plus les autres
    $SUDO pacman -S --needed --noconfirm webkit2gtk-4.1 \
      || $SUDO pacman -S --needed --noconfirm webkit2gtk-4.0 \
      || echo "!! webkit2gtk introuvable: installe webkit2gtk-4.1 manuellement"
    $SUDO pacman -S --needed --noconfirm libappindicator-gtk3 2>/dev/null \
      || echo "(tray optionnel: yay -S libappindicator-gtk3 si tu veux l'icone systeme)"
  elif command -v apt-get >/dev/null; then
    $SUDO apt-get update || true
    $SUDO apt-get install -y python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
      gir1.2-webkit2-4.1 libgtk-3-0 libwebkit2gtk-4.1-0 ffmpeg pipewire pulseaudio \
      libayatana-appindicator3-1 || true
  elif command -v dnf >/dev/null; then
    $SUDO dnf install -y python3 python3-gobject gtk3 webkit2gtk4.1 ffmpeg pipewire pulseaudio || true
  else
    echo "!! distro non reconnue: installe python3, gtk3, webkit2gtk-4.1, python-gobject, ffmpeg, pipewire"
  fi
}

if [ "${1-}" != "--no-system-deps" ]; then install_sys; else echo "(deps systeme ignorees)"; fi

if [ ! -d .venv ]; then echo ">> creation venv (--system-site-packages pour les bindings GTK)"; python3 -m venv --system-site-packages .venv; else echo ">> venv existant conserve"; fi
V="$PWD/.venv/bin"
"$V/python" -m pip install --upgrade pip || true
"$V/pip" install -r requirements.txt
[ -f icon.png ] || "$V/python" make_icon.py || echo "(icon.png absent et make_icon.py introuvable -> logo fallback)"

mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons"
[ -f icon.png ] && cp icon.png "$HOME/.local/share/icons/melodify.png"
ICON="$HOME/.local/share/icons/melodify.png"; [ -f "$ICON" ] || ICON="audio-x-generic"
cat > "$HOME/.local/share/applications/melodify.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Melodify
Comment=Music player
Exec=$V/python $PWD/app.py
Path=$PWD
Icon=$ICON
Terminal=false
Categories=Audio;Music;Player;
StartupWMClass=Melodify
DESK
chmod +x run.sh app.py 2>/dev/null || true
echo; echo "OK. Lance via:  rofi -show drun  (tape Melodify)   ou   ./run.sh"
