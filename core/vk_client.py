import requests
import os

class VKClient:
    def __init__(self, cache_dir="cache"):
        self.access_token = None
        self.user_id = None
        self.cache_dir = cache_dir
        self.api_version = "5.131"
        self.user_agent = "VkMeAndroid/56 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"

        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

    def authenticate(self, token):
        self.access_token = token
        try:
            response = self._call_api("users.get", {})
            if response and isinstance(response, list) and len(response) > 0:
                self.user_id = response[0]['id']
                return True
            return False
        except Exception as e:
            print(f"Auth failed: {e}")
            return False

    def get_audio(self, owner_id=None, count=1000):
        if not self.access_token:
            return []
        
        params = {
            "count": count
        }
        if owner_id:
            params["owner_id"] = owner_id
            
        try:
            data = self._call_api("audio.get", params)
            
            if not data:
                return []
                
            items = data.get('items', [])
            
            tracks = []
            for item in items:
                if 'url' not in item or not item['url']:
                    continue

                image_url = None
                if 'album' in item and 'thumb' in item['album']:
                    album = item['album']
                    if 'thumb' in album and isinstance(album['thumb'], dict):
                        max_res = 0
                        for key, val in album['thumb'].items():
                            if key.startswith('photo_'):
                                try:
                                    res = int(key.split('_')[1])
                                    if res > max_res:
                                        max_res = res
                                        image_url = val
                                except: pass
                    else:
                        max_res = 0
                        for key, val in album.items():
                            if key.startswith('photo_'):
                                try:
                                    res = int(key.split('_')[1])
                                    if res > max_res:
                                        max_res = res
                                        image_url = val
                                except: pass

                tracks.append({
                    'id': item['id'],
                    'owner_id': item['owner_id'],
                    'artist': item['artist'],
                    'title': item['title'],
                    'url': item['url'],
                    'duration': item['duration'],
                    'image_url': image_url
                })
            return tracks
            
        except Exception as e:
            print(f"Error fetching audio: {e}")
            return []

    def download_track(self, track_url):
        print(f"Downloading track...")
        
        mp3_url = track_url
        if 'index.m3u8' in track_url:
            test_url = track_url.replace('index.m3u8', 'index.mp3')
            if self._check_url(test_url):
                mp3_url = test_url
                print("Found direct MP3 link.")

        if 'm3u8' in mp3_url:
            print("Detected M3U8 playlist. Attempting conversion with FFmpeg...")
            return self._download_m3u8_ffmpeg_bytes(mp3_url)
        
        try:
            headers = {"User-Agent": self.user_agent}
            with requests.get(mp3_url, headers=headers, stream=True) as response:
                if response.status_code != 200:
                    print(f"Download failed: Status {response.status_code}")
                    return None
                
                content = response.content
                if content.startswith(b'#EXTM3U'):
                     print("Error: Downloaded file is an M3U8 playlist, not audio.")
                     return None
                     
                return content
                
        except Exception as e:
            print(f"Download failed with error: {e}")
            
        return None

    def download_image(self, url):
        if not url:
            return None
        try:
            response = requests.get(url, headers={"User-Agent": self.user_agent})
            if response.status_code == 200:
                return response.content
        except:
            pass
        return None

    def _check_url(self, url):
        try:
            r = requests.head(url, headers={"User-Agent": self.user_agent})
            return r.status_code == 200
        except:
            return False

    def _download_m3u8_ffmpeg_bytes(self, url):
        import subprocess
        try:
            cmd = ['ffmpeg', '-i', url, '-vn', '-c:a', 'libmp3lame', '-q:a', '2', '-f', 'mp3', '-']
            
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out, err = process.communicate()
            
            if process.returncode == 0 and out:
                return out
            return None
        except FileNotFoundError:
            print("FFmpeg not found. Please install FFmpeg and add it to PATH.")
            return None
        except Exception as e:
            print(f"FFmpeg error: {e}")
            return None

    def _call_api(self, method, params):
        url = f"https://api.vk.com/method/{method}"
        params["access_token"] = self.access_token
        params["v"] = self.api_version
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate"
        }
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        
        if 'error' in data:
            print(f"API Error: {data['error']}")
            raise Exception(data['error'].get('error_msg', 'Unknown error'))
            
        return data.get('response')
