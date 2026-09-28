import secrets
import subprocess

from httpx import Client


class VKClient:
    # VK ID OAuth 2.1: обмен refresh-креденциала на новую пару токенов
    # (см. id.vk.ru -> Документация -> Refresh token / Авторизация без SDK)
    REFRESH_HOSTS = ("https://id.vk.ru", "https://id.vk.com")

    # --- вход по логину/паролю: «Прямая авторизация» (Direct Authorization) --
    # Официальная схема (архивная документация VK, dev.vk.com/api/direct-auth):
    #   POST https://oauth.vk.com/token
    #       grant_type=password&client_id=..&client_secret=..
    #       &username=..&password=..[&scope=..]
    #   ответ: {"access_token": "..", "expires_in": 0, "user_id": ..}
    # со scope=offline токен бессрочный, refresh не нужен.
    # Прямая авторизация доступна только доверенным приложениям, поэтому
    # идём от имени официального приложения «VK для Android» (2274003) —
    # тот же приём использует библиотека vk 3.0 (DirectUserAPI) и другие
    # сторонние клиенты; UA нашего клиента и так мобильный Android.
    # При включённой 2FA VK отвечает:
    #   {"error":"need_validation","error_description":"sms sent, use code param",
    #    "validation_type":"2fa_sms","phone_mask":"+7 *** *** ** 11", ..}
    # → повторяем тот же запрос, добавив code=<код пользователя>.
    # Капча: {"error":"need_captcha","captcha_sid":..,"captcha_img":..} →
    # повтор с captcha_sid + captcha_key.
    DIRECT_AUTH_URL = "https://oauth.vk.com/token"
    DIRECT_CLIENT_ID = "2274003"                # официальное «VK для Android»
    DIRECT_CLIENT_SECRET = "hHbZxrka2uZ6jB1inYsH"
    DIRECT_SCOPE = "audio,offline"               # аудио + бессрочный токен

    # обычный браузерный UA для обмена токенами: id.vk.ru на мобильной
    # UA/HTTP2-клиент отвечает заглушками вместо JSON
    _BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) "
                   "Gecko/20100101 Firefox/153.0")

    def __init__(self):
        self.client = Client(
            http2=True,
            headers={
                "User-Agent": "VkMeAndroid/56 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)",
                "Accept-Encoding": "gzip, deflate"
            }
        )
        self.owner_id = None
        self.access_token = None
        # токены VK ID приходят парой: access (живёт сутки) + refresh
        # (живёт 180 дней), refresh хранится и используется для обновления
        self.refresh_token = None
        self.app_id = None
        self.device_id = None
        self.auth_error = None

        # состояние входа по логину/паролю: что именно запросил VK
        # '2fa'    — need_validation, нужен код (в auth_hint — маска телефона)
        # 'captcha' — need_captcha (в auth_captcha_sid/img — параметры капчи)
        self.auth_need = None
        self.auth_hint = ""        # маска телефона / текст ошибки для формы
        self.auth_captcha_sid = ""
        self.auth_captcha_img = ""

    def _get_image_url(self, value):
        if "album" not in value:
            return None
        if "thumb" not in value["album"]:
            return None

        maxsize = 0
        image_url = None
        for k, v in value["album"]["thumb"].items():
            if not k.startswith("photo_"):
                continue
            kint = int(k[6:])
            if kint > maxsize:
                maxsize = kint
                image_url = v.split("&c_uniq_tag=")[0]

        return image_url

    def _formatter_audio(self, value):
        return [{"track_id": a["id"], "artist": a["artist"], "title": a["title"], "audio_url": a["url"], "image_url": self._get_image_url(a)} for a in value][::-1]

    def call_api(self, method, **params):
        try:
            result = self.client.post(f"https://api.vk.com/method/{method}", params={"v": "5.199", "access_token": self.access_token, **params})
            result = result.json()
            return result["response"] if "response" in result else result
        except:
            return False

    def auth_from_token(self):
        try:
            account = self.call_api("users.get")
        except Exception:
            self.auth_error = None
            return False

        if isinstance(account, list) and account:
            self.owner_id = account[0]["id"]
            self.auth_error = None
            return account[0]["id"]

        # не авторизовались: запоминаем код ошибки VK API — по нему
        # отличаем протухший токен (код 5) от проблем с сетью
        self.auth_error = None
        if isinstance(account, dict):
            err = account.get("error")
            if isinstance(err, dict):
                try:
                    self.auth_error = err.get("error_code")
                except Exception:
                    pass

        return False

    def auth_from_login(self, code="", captcha_sid="", captcha_key=""):

        if not self.login or not self.password:
            print("[VKClient.auth_from_login] Логин и пароль должны быть заполнены")
            return False

        params = {
            "grant_type": "password",
            "client_id": self.DIRECT_CLIENT_ID,
            "client_secret": self.DIRECT_CLIENT_SECRET,
            "username": self.login,
            "password": self.password,
            "scope": self.DIRECT_SCOPE,
            "2fa_supported": 1,
            "v": "5.199",
        }
        if code:
            params["code"] = code
        if captcha_sid:
            params["captcha_sid"] = captcha_sid
            params["captcha_key"] = captcha_key or ""

        self.auth_need = None
        self.auth_hint = ""
        self.auth_captcha_sid = ""
        self.auth_captcha_img = ""

        try:
            r = self.client.post(self.DIRECT_AUTH_URL, params=params, timeout=20.0)
        except Exception as ex:
            print(f"[VKClient.auth_from_login] Сеть: {ex}")
            return False

        try:
            body = r.json()
        except Exception:
            print(f"[VKClient.auth_from_login] Ответ не JSON "
                  f"(HTTP {r.status_code}): {r.text[:300]}")
            return False

        if isinstance(body, dict) and body.get("access_token"):
            self.access_token = body["access_token"]
            # прямая авторизация refresh-токен не выдаёт: с scope=offline
            # токен бессрочный — протухать нечему, refresh-лестница не нужна
            self.refresh_token = ""
            if body.get("user_id"):
                self.owner_id = body["user_id"]
            print(f"[VKClient.auth_from_login] Токен получен "
                  f"(user_id={body.get('user_id')}, "
                  f"expires_in={body.get('expires_in')})")
            return self.auth_from_token()

        err = (body or {}).get("error") if isinstance(body, dict) else None
        desc = (body or {}).get("error_description", "") if isinstance(body, dict) else ""
        print(f"[VKClient.auth_from_login] VK ответил: {err} — {desc}")

        if err == "need_validation":
            self.auth_need = "2fa"
            self.auth_hint = (body.get("phone_mask") or "").strip()
            return False

        if err == "need_captcha":
            self.auth_need = "captcha"
            self.auth_captcha_sid = str(body.get("captcha_sid") or "")
            self.auth_captcha_img = str(body.get("captcha_img") or "")
            return False

        if err == "invalid_grant":
            self.auth_hint = "Неверный логин или пароль"
        return False

    def authenticate(self, auth_type=0, access_token=None, login=None, password=None,
                     refresh_token=None, app_id=None, device_id=None,
                     twofa_code="", captcha_sid="", captcha_key=""):
        self.auth_type = auth_type

        self.auth_need = None
        self.auth_hint = ""
        if auth_type == 0:
            self.access_token = access_token
            self.refresh_token = refresh_token or ""
            self.app_id = app_id or None
            self.device_id = device_id or None
            return self.auth_from_token()
        elif auth_type == 1:
            self.login = login
            self.password = password
            return self.auth_from_login(
                code=twofa_code, captcha_sid=captcha_sid, captcha_key=captcha_key)
        else:
            raise Exception(f"[VKClient.authenticate] Не верный тип авторизации, {auth_type=}")

    def _post_token_exchange(self, data):

        import httpx

        for host in self.REFRESH_HOSTS:
            try:
                r = httpx.post(
                    f"{host}/oauth2/auth", data=data,
                    headers={"User-Agent": self._BROWSER_UA,
                             "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15.0, follow_redirects=True)
                try:
                    return r.json()
                except Exception:
                    print(f"[token refresh] {host}: ответ не JSON "
                          f"(HTTP {r.status_code})")
                    continue
            except Exception as ex:
                print(f"[token refresh] {host}: {ex}")
        return None

    def refresh_access_token(self):

        creds = []
        if self.refresh_token:
            creds.append(self.refresh_token)
        if self.access_token:
            creds.append(self.access_token)

        if not creds:
            return False

        app_id = str(self.app_id or "6287487")
        alphabet = ("abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

        def rnd(n):
            return "".join(secrets.choice(alphabet) for _ in range(n))

        for cred in creds:
            # порядок попыток: известный device_id, без него, сгенерированный
            device_ids = ([self.device_id] if self.device_id else []) + [None]
            generated_tried = False
            err_text = ""

            i = 0
            while i < len(device_ids):
                did = device_ids[i]
                i += 1

                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": cred,
                    "client_id": app_id,
                    "state": rnd(43),
                }
                if did:
                    data["device_id"] = did

                body = self._post_token_exchange(data)

                if body and "access_token" in body:
                    self.access_token = body["access_token"]
                    # VK отдаёт НОВЫЙ refresh_token — цепочка продолжается
                    if body.get("refresh_token"):
                        self.refresh_token = body["refresh_token"]
                    if body.get("device_id"):
                        self.device_id = body["device_id"]
                    print("[token refresh] access_token обновлён "
                          f"(expires_in={body.get('expires_in')})")
                    return True

                err_text = " ".join(filter(None, [
                    str((body or {}).get("error", "")),
                    str((body or {}).get("error_description", ""))]))
                print(f"[token refresh] отклонён (device_id={did}): "
                      f"{err_text.strip()[:200]}")

                # сервис требует device_id — сгенерируем постоянный и повторим
                if (did is None and not generated_tried
                        and "device" in err_text.lower()):
                    generated_tried = True
                    self.device_id = rnd(22)
                    device_ids.append(self.device_id)

        return False

    def get_audio(self, count=800, rexit=False):
        audios = self.call_api("audio.get", count=count)
        if audios is False:
            raise Exception("[VKClient.get_audio] Не удалось получить список треков.")
        if rexit:
            return self._formatter_audio(audios["items"])

        count_audio = audios["count"]
        parsed_audio = audios["items"]

        while len(parsed_audio) < count_audio:
            audios = self.call_api("audio.get", count=count, offset=len(parsed_audio))
            if audios is False:
                raise Exception("[VKClient.get_audio] Не удалось получить список треков.")
            if len(audios["items"]) < 1:
                break
            parsed_audio.extend(audios["items"])

        return self._formatter_audio(parsed_audio)

    def get_audio_data(self, url):
        if not url:
            raise Exception("[VKClient.get_audio_data] Не удалось скачать трек -> Отсутствует ссылка")
        if "index.m3u8" not in url:
            raise Exception("[VKClient.get_audio_data] Не удалось скачать трек -> Не верная ссылка")

        try:
            process = subprocess.Popen(
                ["ffmpeg", "-i", url, "-vn", "-c:a", "copy", "-f", "mp3", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            out, err = process.communicate()
            if process.returncode == 0 and out:
                return out
            return None
        except Exception as ex:
            raise Exception(f"[VKClient.get_audio_data] Не удалось скачать трек -> {ex}")

    def get_audio_image(self, url):
        if not url:
            raise Exception("[VKClient.get_audio_image] Не удалось скачать изображение -> Отсутствует ссылка")

        try:
            result = self.client.get(url, params=None, headers={"User-Agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0'})
            if result.status_code != 200:
                raise Exception(f"status_code={result.status_code}")
            return result.read()
        except Exception as ex:
            raise Exception(f"[VKClient.get_audio_image] Не удалось скачать изображение -> {ex}")
