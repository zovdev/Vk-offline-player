# VK Offline Player

A modern, offline-capable music player for VK (VKontakte) built with Python and PySide6.

## Features

*   **Offline Playback**: Downloads tracks and stores them locally in a SQLite database.
*   **High-Quality Audio**: Supports MP3 and M3U8 stream conversion.
*   **Album Art**: Fetches and displays high-resolution album art.
*   **Audio Effects**:
    *   10-Band Equalizer
    *   Speed Control (0.0x - 3.0x) with pitch correction
    *   Multi-band Limiter/Compressor for consistent volume
*   **Export**: Export processed tracks (with EQ and speed effects applied) to MP3.
*   **Modern UI**: Dark theme with a responsive and clean interface.

## Installation

1.  Clone the repository:
    ```bash
    git clone https://github.com/yourusername/vk-offline-player.git
    cd vk-offline-player
    ```

2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

    *Note: You will need `ffmpeg` installed and added to your system PATH for M3U8 stream support.*

## Usage

1.  Run the application:
    ```bash
    python main.py
    ```

2.  **Authentication**:
    *   Enter your VK Access Token in the input field.
    *   Click "Load Tracks".

3.  **Playback**:
    *   Double-click a track to play.
    *   Use the controls at the bottom to pause, seek, and adjust volume.

4.  **Effects**:
    *   Adjust the 10-band equalizer to customize the sound.
    *   Use the speed slider to change playback speed without altering pitch.

5.  **Export**:
    *   Select a track.
    *   Click "Export Processed Track" to save a version with your current effects applied.

## Requirements

*   Python 3.8+
*   PySide6
*   sounddevice
*   soundfile
*   numpy
*   requests
*   pedalboard
*   pydub

## License

MIT License
