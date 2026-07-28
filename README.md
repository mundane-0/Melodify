# Melodify

A music player that downloads songs from Spotify, YouTube, SoundCloud, and Bandcamp and stores them locally; you can even download playlists (Spotify only), and once downloaded, the songs work offline. In the app, you can customize EVERYTHING—the theme, colors, accent color, switch to glass mode or minimalist mode, hide any interface element (the top bar, the right panel, the left column, the album art—basically anything you want), adjust the album art size, and there’s a 5-band equalizer and playback speed control. The app is available in French and English.

There are synchronized lyrics that scroll in time with the music and several display modes (classic, immersive, ambient), and you can even adjust the lyrics track by track if they’re out of sync (and the app remembers this on its own), and there’s a visualizer with bars that move to the beat, plus a Pulse mode where the album art and lyrics react to the music with sparks, waves, and a pulsing halo.

There are also your listening stats—kind of like Spotify Wrapped but better and available all the time—so you can see how long you’ve listened during the current session, week, and month, your favorite artist, your favorite song, your most-played playlist, and you can even go back and check previous weeks and months.

And it displays what you're listening to right on Discord (Rich Presence, enabled by default—you can choose what's displayed: the title, artist, or album art), and the music automatically turns down when you speak or when there’s sound coming from somewhere else (you can adjust the percentage), and there’s a small icon in the system tray to control the music without opening the window (play, pause, next, previous), and you can even open the app multiple times (all windows share the same library).

#

## Installation
```bash
git clone https://github.com/mundane-0/Melodify.git
cd Melodify
chmod +x install.sh
./install.sh            # Add --no-system-deps to skip system packages
