#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mymind-archive
==============
mymind の中身を、公式API (https://access.mymind.com/api) で毎日手元に写していく。

方針
  * 手元からは消さない。mymind 側で消えたものは「削除日」を書き足して残す。
  * 更新されたものは最新の状態で上書きする。
  * 文章 (Markdown) と生データ (JSON) は、一覧の 1 リクエストで全部取れる。
    画像・PDF などは 1 件ずつ取るので、API クレジットの残りを見ながら進め、
    足りなければ止まって次回に続きから再開する。
  * データはこのリポジトリの外に置く (既定は ~/mymind-archive)。
    設定は ~/.config/mymind-archive/config.json、API キーは macOS のキーチェーン。
    どれもリポジトリには入らない。データ置き場がリポジトリの中を指していたら止まる。
  * config.json の mirror_dir を設定すると、そこ (Dropbox など) へ一方通行で写す。
    写し側で消えたファイルは次回また写る。

使い方
  backup.py            いつもの実行 (launchd から毎日呼ぶ)
  backup.py --probe    お試し。認証と件数、消費クレジットを確かめるだけで何も書かない
  backup.py --no-media 文章だけ。画像・原本は取りに行かない
  backup.py --rebuild  通信なし。手元の生データから Markdown を作り直す

Python 3.9 以上 / 標準ライブラリのみ。
"""

import argparse
import base64
import fcntl
import gzip
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "1.0.0"
USER_AGENT = "mymind-archive/" + VERSION
API_BASE = os.environ.get("MYMIND_API_BASE", "https://api.mymind.com").rstrip("/")
CONFIG_DIR = os.environ.get("MYMIND_ARCHIVE_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~"), ".config", "mymind-archive")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
KEYCHAIN_SERVICE = "mymind-archive"
DATA = None  # データの置き場。設定を読んだあと set_data_dir() で決まる

DEFAULT_CONFIG = {
    # データの置き場 (このリポジトリの外にすること)
    "data_dir": "~/mymind-archive",
    # 写し先 (例: ~/Library/CloudStorage/Dropbox/mymind-archive)。null なら写さない
    "mirror_dir": None,
    # mymind が持っているベクトル (embeddings) も保存する
    "include_embeddings": True,
    "download_screenshots": True,
    "download_blobs": True,
    # 画像 URL が直接取れるならそちらを先に試す (API クレジットの節約)
    "try_direct_media_url": True,
    # 30日枠の残りがこの割合を切ったら、画像・原本の取得をやめて次回に回す
    "sustained_reserve_ratio": 0.3,
    # これより大きい原本ファイルは取らない (MB)
    "max_blob_mb": 200,
    # 一覧から一度に消えた割合がこれを超えたら、API の不調を疑って削除扱いにしない
    "mass_delete_guard": 0.3,
}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic", ".bmp", ".tif", ".tiff", ".svg"}
EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "image/heic": ".heic", "image/svg+xml": ".svg",
    "application/pdf": ".pdf", "video/mp4": ".mp4", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "text/plain": ".txt", "text/markdown": ".md",
}
REDIRECT_CODES = (301, 302, 303, 307, 308)
# (シークレットを base64 デコードして鍵にするか, path クレームにクエリを含めるか)
AUTH_MODES = [(True, False), (False, False), (True, True), (False, True)]


# ---------------------------------------------------------------- 小道具

def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?\s*(Z|[+-]\d{2}:?\d{2})?$",
        s.strip(),
    )
    if not m:
        return None
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    sec = int(m.group(6) or 0)
    micro = int((m.group(7) or "0")[:6].ljust(6, "0"))
    tz = m.group(8)
    if not tz or tz == "Z":
        tzinfo = timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        tzinfo = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:])))
    try:
        return datetime(y, mo, d, h, mi, sec, micro, tzinfo=tzinfo)
    except ValueError:
        return None


def local_dt(s):
    dt = parse_iso(s) or datetime.now(timezone.utc)
    return dt.astimezone()


def local_day(s):
    dt = parse_iso(s)
    return dt.astimezone().strftime("%Y-%m-%d") if dt else "?"


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, bytes):
        with open(tmp, "wb") as f:
            f.write(data)
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    os.replace(tmp, path)


def write_if_changed(path, text):
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except (OSError, UnicodeDecodeError):
        pass
    write_atomic(path, text)
    return True


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = load_json(CONFIG_PATH, {}) or {}
    cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    if os.environ.get("MYMIND_ARCHIVE_DIR"):
        cfg["data_dir"] = os.environ["MYMIND_ARCHIVE_DIR"]
    if os.environ.get("MYMIND_MIRROR_DIR"):
        cfg["mirror_dir"] = os.environ["MYMIND_MIRROR_DIR"]
    cfg["data_dir"] = os.path.abspath(os.path.expanduser(cfg["data_dir"]))
    if cfg["mirror_dir"]:
        cfg["mirror_dir"] = os.path.abspath(os.path.expanduser(cfg["mirror_dir"]))
    return cfg


def _inside(child, parent):
    return (os.path.realpath(child) + os.sep).startswith(os.path.realpath(parent) + os.sep)


def code_repo_root():
    """このスクリプトが入っている git リポジトリの場所 (なければ None)。"""
    p = os.path.dirname(os.path.realpath(__file__))
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def set_data_dir(cfg):
    """データ置き場を決める。コードのリポジトリの中なら、誤って公開しないよう止める。"""
    global DATA
    repo = code_repo_root()
    for key in ("data_dir", "mirror_dir"):
        d = cfg.get(key)
        if d and repo and _inside(d, repo):
            raise SystemExit(
                "%s がコードのリポジトリの中を指しています: %s\n"
                "データを誤ってコミットしないよう、リポジトリの外を指定してください。" % (key, d))
    m = cfg.get("mirror_dir")
    if m and (_inside(m, cfg["data_dir"]) or _inside(cfg["data_dir"], m)):
        raise SystemExit("mirror_dir と data_dir は別々の場所にしてください")
    DATA = cfg["data_dir"]


# ---------------------------------------------------------------- 認証情報

def keychain_get(account):
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def load_credentials():
    kid, secret = os.environ.get("MYMIND_KID"), os.environ.get("MYMIND_SECRET")
    if kid and secret:
        return kid.strip(), secret.strip()
    kid, secret = keychain_get("kid"), keychain_get("secret")
    if kid and secret:
        return kid, secret
    path = os.path.join(CONFIG_DIR, "credentials.json")
    if os.path.exists(path):
        if os.stat(path).st_mode & 0o077:
            raise SystemExit("credentials.json が他のユーザーから読める状態です。chmod 600 してください: " + path)
        d = load_json(path, {})
        if d.get("kid") and d.get("secret"):
            return d["kid"].strip(), d["secret"].strip()
    raise SystemExit(
        "mymind の API キーが見つかりません。キーチェーンに登録してください:\n"
        "  security add-generic-password -U -s mymind-archive -a kid -w\n"
        "  security add-generic-password -U -s mymind-archive -a secret -w"
    )


# ---------------------------------------------------------------- API クライアント

class ApiError(Exception):
    def __init__(self, status, detail=""):
        super().__init__("HTTP %s %s" % (status, detail))
        self.status = status
        self.detail = detail


class AuthError(ApiError):
    pass


class StopForQuota(Exception):
    """30日枠のクレジットが尽きた。今回はここまで。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _secret_key(secret, decode):
    if decode:
        try:
            s = secret.strip().replace("-", "+").replace("_", "/")
            s += "=" * (-len(s) % 4)
            return base64.b64decode(s, validate=True)
        except (ValueError, base64.binascii.Error):
            pass
    return secret.encode("utf-8")


def make_token(kid, secret, method, path, query_string, mode):
    decode, with_query = AUTH_MODES[mode]
    claim_path = path + ("?" + query_string if (with_query and query_string) else "")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload = {"path": claim_path, "method": method.upper(), "iat": now, "exp": now + 300}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(_secret_key(secret, decode), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def parse_ratelimit(value):
    """'"burst";r=9990;t=300, "sustained";r=99641;t=2589945' -> {'burst': {'r':..,'t':..}, ...}"""
    out = {}
    for part in (value or "").split(","):
        m = re.match(r'\s*"?([A-Za-z0-9_.-]+)"?((?:\s*;\s*\w+=\d+)*)', part)
        if not m or not m.group(1):
            continue
        out[m.group(1)] = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(2))}
    return out


class Client:
    def __init__(self, kid, secret, state, cfg):
        self.kid, self.secret, self.state, self.cfg = kid, secret, state, cfg
        self.mode = state.get("auth_mode")
        if not isinstance(self.mode, int) or not (0 <= self.mode < len(AUTH_MODES)):
            self.mode = None
        self.api = urllib.request.build_opener(_NoRedirect)
        self.plain = urllib.request.build_opener()
        self.limits = {}
        self.cost = 0
        self.last_cost = None
        self.requests = 0
        self.direct_failures = 0

    # --- クレジットの残り
    def _note(self, headers):
        if headers is None:
            return
        for name, d in parse_ratelimit(headers.get("RateLimit-Policy")).items():
            self.limits.setdefault(name, {}).update({k: d[k] for k in ("q", "w") if k in d})
        for name, d in parse_ratelimit(headers.get("RateLimit")).items():
            self.limits.setdefault(name, {}).update({k: d[k] for k in ("r", "t") if k in d})
        c = headers.get("RateLimit-Cost")
        self.last_cost = None
        if c and c.strip().isdigit():
            self.last_cost = int(c.strip())
            self.cost += self.last_cost

    def can_spend(self):
        s = self.limits.get("sustained") or {}
        if "r" in s and "q" in s:
            return s["r"] >= s["q"] * float(self.cfg["sustained_reserve_ratio"])
        return True

    def wait_for_burst(self):
        b = self.limits.get("burst") or {}
        if "r" in b and b["r"] < 60:
            wait = min(int(b.get("t", 300)), 330) + 1
            log("短時間枠の残りが少ないので %d 秒待ちます" % wait)
            time.sleep(wait)
            b.pop("r", None)

    def quota_text(self):
        parts = []
        for name in ("burst", "sustained"):
            d = self.limits.get(name) or {}
            if "r" in d:
                parts.append("%s 残り %s/%s" % ("短時間枠" if name == "burst" else "30日枠", d["r"], d.get("q", "?")))
        return "、".join(parts) or "残量ヘッダーなし"

    # --- リクエスト
    def _request_once(self, method, path, qs, accept, mode):
        url = API_BASE + path + ("?" + qs if qs else "")
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": "Bearer " + make_token(self.kid, self.secret, method, path, qs, mode),
            "User-Agent": USER_AGENT,
            "Accept": accept,
        })
        self.requests += 1
        return self.api.open(req, timeout=120)

    def open(self, method, path, query=None, accept="application/json"):
        """レスポンスを返す。リダイレクトのときは HTTPError (code=30x) をそのまま返す。"""
        qs = urllib.parse.urlencode(query or {})
        modes = [self.mode] if self.mode is not None else list(range(len(AUTH_MODES)))
        reprobed = False
        i = 0
        while i < len(modes):
            mode = modes[i]
            attempt = 0
            while True:
                attempt += 1
                try:
                    resp = self._request_once(method, path, qs, accept, mode)
                    self._note(resp.headers)
                    self._remember_mode(mode)
                    return resp
                except urllib.error.HTTPError as e:
                    self._note(e.headers)
                    if e.code in REDIRECT_CODES:
                        self._remember_mode(mode)
                        return e
                    body = _read_error(e)
                    if e.code == 401:
                        break  # 次の署名方式を試す
                    if e.code == 429:
                        s = self.limits.get("sustained") or {}
                        b = self.limits.get("burst") or {}
                        if s.get("r", 1) <= 0 or attempt >= 3:
                            raise StopForQuota(body)
                        wait = min(int(b.get("t", 60)), 330) + 1
                        log("429 (クレジット切れ)。%d 秒待って続けます" % wait)
                        time.sleep(wait)
                        continue
                    if e.code in (500, 502, 503, 504) and attempt < 4:
                        time.sleep(5 * attempt)
                        continue
                    raise ApiError(e.code, body)
                except (urllib.error.URLError, OSError) as e:
                    if attempt < 4:
                        time.sleep(5 * attempt)
                        continue
                    raise ApiError(0, "通信エラー: %s" % e)
            # ここに来るのは 401 のときだけ
            i += 1
            if i >= len(modes) and self.mode is not None and not reprobed:
                # 覚えていた署名方式が通らなくなった。全方式を試し直す
                reprobed = True
                self.mode = None
                modes = list(range(len(AUTH_MODES)))
                i = 0
        raise AuthError(401, "認証に失敗しました。キーID・シークレットを確かめてください")

    def _remember_mode(self, mode):
        if self.mode != mode:
            self.mode = mode
            self.state["auth_mode"] = mode

    def get_json(self, path, query=None):
        resp = self.open("GET", path, query)
        try:
            if getattr(resp, "code", 200) in REDIRECT_CODES:
                raise ApiError(resp.code, "予期しないリダイレクト")
            return json.loads(_read_body(resp).decode("utf-8"))
        finally:
            resp.close()

    def iter_objects(self, query):
        """全件を 1 リクエストで流してもらう (JSONL)。"""
        resp = self.open("GET", "/objects", query, accept="application/jsonl")
        try:
            if getattr(resp, "code", 200) in REDIRECT_CODES:
                raise ApiError(resp.code, "予期しないリダイレクト")
            ctype = (resp.headers.get("Content-Type") or "").lower()
            gz = "gzip" in (resp.headers.get("Content-Encoding") or "").lower()
            if gz or not ("jsonl" in ctype or "ndjson" in ctype):
                data = _read_body(resp).decode("utf-8").strip()
                if not data:
                    return
                try:
                    parsed = json.loads(data)
                except ValueError:
                    parsed = [json.loads(line) for line in data.splitlines() if line.strip()]
                if isinstance(parsed, dict):
                    parsed = parsed.get("objects") or parsed.get("data") or [parsed]
                for obj in parsed:
                    if isinstance(obj, dict):
                        yield obj
                return
            expected = resp.headers.get("Content-Length")
            got = 0
            for line in resp:
                got += len(line)
                line = line.strip()
                if line:
                    obj = json.loads(line.decode("utf-8"))
                    if isinstance(obj, dict):
                        yield obj
            if expected and expected.isdigit() and got < int(expected):
                raise ApiError(0, "一覧が途中で切れました (%d/%s バイト)" % (got, expected))
        finally:
            resp.close()

    # --- ファイル
    def _save_stream(self, resp, dest, max_bytes):
        length = resp.headers.get("Content-Length")
        if max_bytes and length and length.isdigit() and int(length) > max_bytes:
            raise ApiError(413, "大きすぎるので取りません (%d MB)" % (int(length) // 1048576))
        size = 0
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes and size > max_bytes:
                    raise ApiError(413, "大きすぎるので取りません")
                f.write(chunk)
        if size == 0:
            raise ApiError(0, "中身が空でした")
        return (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower(), size

    def fetch_plain(self, url, dest, max_bytes=None):
        """認証なしで URL を取る (CDN 用)。"""
        if not url.lower().startswith("https://") and not url.startswith(API_BASE):
            raise ApiError(0, "https 以外の URL は取りません")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            resp = self.plain.open(req, timeout=180)
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, _read_error(e))
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, "通信エラー: %s" % e)
        try:
            return self._save_stream(resp, dest, max_bytes)
        finally:
            resp.close()

    def download(self, path, dest, max_bytes=None):
        """API 経由でファイルを取る。CDN へのリダイレクトには認証ヘッダーを付けずに付いていく。"""
        resp = self.open("GET", path, accept="*/*")
        try:
            if getattr(resp, "code", 200) in REDIRECT_CODES:
                loc = resp.headers.get("Location")
                if not loc:
                    raise ApiError(resp.code, "リダイレクト先がありません")
                return self.fetch_plain(urllib.parse.urljoin(API_BASE + path, loc), dest, max_bytes)
            return self._save_stream(resp, dest, max_bytes)
        finally:
            resp.close()


def _read_body(resp):
    data = resp.read()
    if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
        data = gzip.decompress(data)
    return data


def _read_error(e):
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        d = json.loads(raw)
        return "%s %s" % (d.get("type", ""), d.get("detail", ""))
    except ValueError:
        return raw[:200]


# ---------------------------------------------------------------- オブジェクトの読み方

def obj_url(obj):
    src = obj.get("source")
    return obj.get("url") or (src.get("url") if isinstance(src, dict) else None)


def obj_kind(obj):
    me = obj.get("mainEntity")
    if isinstance(me, dict) and (me.get("@type") or me.get("type")):
        return me.get("@type") or me.get("type")
    return obj.get("entityType") or obj.get("type")


def obj_summary(obj):
    s = obj.get("summary")
    if not s and isinstance(obj.get("ai"), dict):
        s = obj["ai"].get("summary")
    return s if isinstance(s, str) else None


def _strip_volatile(v, parent=None):
    """比較用。毎回変わりうる署名付き URL とベクトルは外す。"""
    if isinstance(v, dict):
        return {
            k: _strip_volatile(x, k) for k, x in v.items()
            if k != "embeddings" and not (parent in ("blob", "screenshot", "thumbnail") and k == "url")
        }
    if isinstance(v, list):
        return [_strip_volatile(x, parent) for x in v]
    return v


def obj_hash(obj):
    s = json.dumps(_strip_volatile(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def prose_to_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(prose_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("text"), str):
        return node["text"]
    inner = prose_to_text(node.get("content") or [])
    t = str(node.get("type") or "")
    if t in ("hardBreak", "hard_break"):
        return "\n"
    if t in ("paragraph", "heading", "blockquote", "listItem", "list_item", "codeBlock", "code_block"):
        return inner + "\n\n"
    return inner


def body_text(c):
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        b = c.get("body", c)
        if isinstance(b, str):
            return b.strip()
        return prose_to_text(b).strip()
    return ""


def safe_title(title, maxlen=60):
    t = unicodedata.normalize("NFC", title or "")
    t = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|#%\[\]]+', " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:maxlen].rstrip(" .") or "untitled"


def media_ref(v):
    if not v:
        return None
    if isinstance(v, str):
        return v.split("?")[0]
    if isinstance(v, dict):
        return v.get("path") or (v.get("url") or "").split("?")[0] or "present"
    return "present"


def ext_for(ctype, name=None, url=None):
    if name:
        e = os.path.splitext(name)[1].lower()
        if 1 < len(e) <= 6:
            return e
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    e = mimetypes.guess_extension(ctype) if ctype else None
    if e:
        return e
    if url:
        e = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if 1 < len(e) <= 6:
            return e
    return ".bin"


# ---------------------------------------------------------------- 手元のファイル

def item_dir(st):
    return os.path.join(DATA, "items", st["dir"])


def md_path(st):
    return os.path.join(item_dir(st), st["base"] + ".md")


def raw_path(oid):
    return os.path.join(DATA, "raw", oid + ".json")


def emb_path(oid):
    return os.path.join(DATA, "raw", oid + ".embeddings.json")


def yaml_str(s):
    return json.dumps(str(s), ensure_ascii=False)


def build_markdown(obj, st, spaces_map):
    title = obj.get("title") or "(無題)"
    url = obj_url(obj)
    kind = obj_kind(obj)
    tag_objs = [t for t in (obj.get("tags") or []) if isinstance(t, dict) and t.get("name")]
    tags = [t["name"] for t in tag_objs]
    manual = [t["name"] for t in tag_objs if _flags(t) & 8]
    spaces = []
    for s in obj.get("spaces") or []:
        sid = s.get("id") if isinstance(s, dict) else s
        name = spaces_map.get(sid) or (s.get("name") if isinstance(s, dict) else None) or sid
        if name:
            spaces.append(name)

    fm = ["---", "id: " + str(obj.get("id")), "title: " + yaml_str(title)]
    if kind:
        fm.append("type: " + yaml_str(kind))
    if url:
        fm.append("url: " + yaml_str(url))
    fm.append("tags: [" + ", ".join(yaml_str(t) for t in tags) + "]")
    if manual:
        fm.append("tags_manual: [" + ", ".join(yaml_str(t) for t in manual) + "]")
    if spaces:
        fm.append("spaces: [" + ", ".join(yaml_str(s) for s in spaces) + "]")
    for k in ("created", "modified", "bumped"):
        if obj.get(k):
            fm.append("%s: %s" % (k, obj[k]))
    if st.get("deleted"):
        fm.append("status: deleted")
        fm.append("deleted: " + st["deleted"])
        if st.get("deleted_how") == "detected":
            fm.append("deleted_note: " + yaml_str("正確な削除日時は不明。消えたことに気づいた日時"))
    else:
        fm.append("status: active")
    fm.append("---")

    out = fm + ["", "# " + title.replace("\n", " "), ""]
    if st.get("deleted"):
        out += ["mymindから削除済み（%s）" % local_day(st["deleted"]), ""]
    if url:
        out += ["<%s>" % url, ""]
    summary = obj_summary(obj)
    if summary:
        out += ["> " + ln for ln in summary.strip().splitlines()] + [""]

    media = st.get("media") or {}
    image = None
    for kind_ in ("blob", "screenshot"):
        f = (media.get(kind_) or {}).get("file")
        if f and (media[kind_].get("status") == "done") and os.path.splitext(f)[1].lower() in IMAGE_EXT:
            image = f
            break
    if image:
        out += ["![](<%s>)" % image, ""]
    blob_file = (media.get("blob") or {}).get("file")
    if blob_file and media["blob"].get("status") == "done" and blob_file != image:
        out += ["原本ファイル: [%s](<%s>)" % (blob_file, blob_file), ""]

    notes = [body_text(n.get("content") if isinstance(n, dict) else n) for n in (obj.get("notes") or [])]
    notes = [n for n in notes if n]
    if notes:
        out += ["## メモ", ""]
        for n in notes:
            out += [n, ""]
    content = body_text(obj.get("content"))
    if content:
        out += ["## 本文", "", content, ""]
    return "\n".join(out).rstrip() + "\n"


def _flags(t):
    try:
        return int(t.get("flags") or 0)
    except (TypeError, ValueError):
        return 0


def write_raw(obj):
    oid = obj["id"]
    emb = obj.get("embeddings")
    slim = {k: v for k, v in obj.items() if k != "embeddings"}
    write_if_changed(raw_path(oid), json.dumps(slim, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if emb is not None:
        write_if_changed(emb_path(oid), json.dumps(emb, separators=(",", ":")) + "\n")


def plan_media(obj, st, cfg):
    media = st.setdefault("media", {})
    for kind, enabled in (("screenshot", cfg["download_screenshots"]), ("blob", cfg["download_blobs"])):
        if not enabled:
            continue
        ref = media_ref(obj.get(kind))
        if ref is None:
            continue
        cur = media.get(kind)
        if cur is None or cur.get("ref") != ref:
            media[kind] = {"ref": ref, "status": "pending", "tries": 0, "file": (cur or {}).get("file")}


def fetch_media(client, oid, kind, info, obj, st, cfg):
    meta = obj.get(kind) if isinstance(obj.get(kind), dict) else {}
    base = os.path.join(item_dir(st), st["base"] + (".screenshot" if kind == "screenshot" else ""))
    tmp = base + ".part"
    max_bytes = int(float(cfg["max_blob_mb"]) * 1048576)
    url = meta.get("url")
    ctype = None
    got = False
    try:
        if url and cfg["try_direct_media_url"] and client.direct_failures < 3:
            try:
                ctype, size = client.fetch_plain(url, tmp, max_bytes)
                got = not (ctype or "").startswith("text/html")
                client.direct_failures = 0 if got else client.direct_failures + 1
            except ApiError as e:
                if e.status == 413:
                    raise
                client.direct_failures += 1
        if not got:
            ctype, size = client.download("/objects/%s/%s" % (oid, kind), tmp, max_bytes)
        ext = ext_for(meta.get("type") or ctype, meta.get("name"), url)
        final = base + ext
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    info.update(status="done", file=os.path.basename(final), bytes=size)
    info.pop("error", None)


# ---------------------------------------------------------------- 状態・一覧

def load_state():
    st = load_json(os.path.join(DATA, "state.json"))
    if st is None:
        st = {"version": 1, "objects": {}}
    st.setdefault("objects", {})
    return st


def save_state(state):
    write_atomic(os.path.join(DATA, "state.json"), json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))


def acquire_lock():
    os.makedirs(DATA, exist_ok=True)
    f = open(os.path.join(DATA, ".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("ほかの実行が動いています。終わってからやり直してください")
    return f


def write_index(state):
    rows = []
    for oid, st in state["objects"].items():
        rows.append({
            "id": oid, "title": st.get("title"), "type": st.get("kind"), "url": st.get("url"),
            "created": st.get("created"), "modified": st.get("modified"), "deleted": st.get("deleted"),
            "path": "items/%s/%s.md" % (st["dir"], st["base"]),
        })
    rows.sort(key=lambda r: (r.get("created") or "", r["id"]), reverse=True)
    write_if_changed(
        os.path.join(DATA, "index.jsonl"),
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
    )
    gone = sorted([r for r in rows if r["deleted"]], key=lambda r: r["deleted"], reverse=True)
    lines = ["# mymindから消したもの", "", "新しく消した順、%d件。" % len(gone), ""]
    for r in gone:
        title = (r["title"] or "(無題)").replace("[", "(").replace("]", ")").replace("\n", " ")
        lines.append("- %s 削除 / %s 保存 — [%s](<%s>)" % (
            local_day(r["deleted"]), local_day(r["created"]), title, r["path"]))
    write_if_changed(os.path.join(DATA, "deleted.md"), "\n".join(lines) + "\n")


def mirror(src, dst):
    """src を dst へ一方通行で写す。dst 側のファイルは消さない。"""
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in filenames:
            if fn.startswith(".") or fn.endswith((".tmp", ".part")):
                continue
            s, d = os.path.join(dirpath, fn), os.path.join(target, fn)
            try:
                ss = os.stat(s)
                try:
                    ds = os.stat(d)
                    same = ds.st_size == ss.st_size and abs(ds.st_mtime - ss.st_mtime) < 2
                except FileNotFoundError:
                    same = False
                if not same:
                    shutil.copy2(s, d)
                    copied += 1
            except OSError as e:
                log("写しに失敗: %s (%s)" % (d, e))
    return copied


# ---------------------------------------------------------------- 本体

def apply_object(obj, state, cfg, spaces_map, stats, now_iso, force_write=False):
    """一覧や個別取得で届いた 1 件を手元に反映する。"""
    oid = obj["id"]
    objs = state["objects"]
    st = objs.get(oid)
    h = obj_hash(obj)
    changed = force_write
    if st is None:
        created = local_dt(obj.get("created"))
        st = {
            "base": "%s_%s_%s" % (created.strftime("%Y%m%d"), safe_title(obj.get("title")), oid[-6:]),
            "dir": created.strftime("%Y-%m"),
            "first_seen": now_iso,
            "media": {},
        }
        objs[oid] = st
        changed = True
        stats["new"] += 1
    elif st.get("hash") != h:
        changed = True
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    if changed:
        st.update(
            hash=h, title=obj.get("title"), kind=obj_kind(obj), url=obj_url(obj),
            created=obj.get("created"), modified=obj.get("modified"),
        )
        write_raw(obj)
    elif obj.get("embeddings") is not None and not os.path.exists(emb_path(oid)):
        write_raw(obj)
    plan_media(obj, st, cfg)
    if changed or not os.path.exists(md_path(st)):
        write_if_changed(md_path(st), build_markdown(obj, st, spaces_map))
    return st


def rerender(oid, st, spaces_map):
    obj = load_json(raw_path(oid))
    if obj:
        write_if_changed(md_path(st), build_markdown(obj, st, spaces_map))
    return obj


def run(args):
    cfg = load_config()
    set_data_dir(cfg)
    lock = acquire_lock()
    state = load_state()
    objs = state["objects"]
    spaces_map = dict(state.get("spaces") or {})
    now_iso = utcnow_iso()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "deleted": 0, "restored": 0,
             "media": 0, "media_pending": 0, "mirrored": 0}
    note = []

    if args.rebuild:
        n = 0
        for oid, st in objs.items():
            if rerender(oid, st, spaces_map):
                n += 1
        write_index(state)
        log("Markdown を作り直しました: %d 件" % n)
        return 0

    kid, secret = load_credentials()
    client = Client(kid, secret, state, cfg)
    log("開始 (v%s) 手元 %d 件" % (VERSION, len(objs)))

    # 1) スペース名
    try:
        for s in client.get_json("/spaces"):
            if isinstance(s, dict) and s.get("id"):
                spaces_map[s["id"]] = s.get("name")
        state["spaces"] = spaces_map
    except AuthError as e:
        log(str(e))
        return 2
    except (ApiError, StopForQuota, ValueError) as e:
        log("スペース一覧は取れませんでした (続行): %s" % e)

    # 2) 全件の一覧。文章と生データはここで全部そろう
    query = {"contentAs": "text/markdown"}
    if cfg["include_embeddings"]:
        query["include"] = "embeddings"
    seen = set()
    complete = False
    try:
        for obj in client.iter_objects(query):
            oid = obj.get("id")
            if not isinstance(oid, str) or not re.match(r"^[A-Za-z0-9_-]{6,64}$", oid):
                continue
            seen.add(oid)
            st = objs.get(oid)
            if st is not None and st.get("deleted") and not obj.get("deleted"):
                st["deleted"] = None
                st.pop("deleted_how", None)
                st["hash"] = None  # 書き直させる
                stats["restored"] += 1
                stats["updated"] -= 1
            st = apply_object(obj, state, cfg, spaces_map, stats, now_iso)
            st["last_seen"] = now_iso
            if len(seen) % 200 == 0:
                save_state(state)
        complete = True
    except AuthError as e:
        log(str(e))
        return 2
    except StopForQuota:
        note.append("一覧の途中でクレジットが尽きました")
    except (ApiError, ValueError, OSError, http.client.HTTPException) as e:
        note.append("一覧の取得が途中で失敗: %s" % e)
    save_state(state)
    log("一覧 %d 件 (新規 %d / 更新 %d / 変化なし %d / 復活 %d) 消費 %s  %s" % (
        len(seen), stats["new"], stats["updated"], stats["unchanged"], stats["restored"],
        client.cost, client.quota_text()))

    # 3) 一覧から消えたもの = mymind で削除されたもの
    if complete:
        active = [oid for oid, st in objs.items() if not st.get("deleted")]
        missing = [oid for oid in active if oid not in seen]
        limit = max(20, float(cfg["mass_delete_guard"]) * len(active))
        if missing and not args.accept_mass_delete and len(missing) > limit:
            note.append("%d 件が一覧から消えています。多すぎるので今回は削除扱いにしていません "
                        "(本当に消したなら --accept-mass-delete を付けて実行)" % len(missing))
            missing = []
        for oid in missing:
            st = objs[oid]
            try:
                client.wait_for_burst()
                obj = client.get_json("/objects/" + oid, {"contentAs": "text/markdown"})
            except StopForQuota:
                note.append("削除の確認の途中でクレジットが尽きました")
                break
            except ApiError as e:
                if e.status != 404:
                    log("削除の確認に失敗 %s: %s" % (oid, e))
                    continue
                obj = None
            if isinstance(obj, dict) and obj.get("id") == oid and not obj.get("deleted"):
                continue  # まだある。一覧の一時的な抜けとみなす
            if isinstance(obj, dict) and obj.get("id") == oid:
                st["deleted"] = obj.get("deleted") if isinstance(obj.get("deleted"), str) else now_iso
                st["deleted_how"] = "api"
                st["hash"] = obj_hash(obj)
                st.update(title=obj.get("title"), modified=obj.get("modified"))
                write_raw(obj)  # 消える直前の最後の状態
                plan_media(obj, st, cfg)
                write_if_changed(md_path(st), build_markdown(obj, st, spaces_map))
            else:
                st["deleted"] = now_iso
                st["deleted_how"] = "detected"
                rerender(oid, st, spaces_map)
            stats["deleted"] += 1
        save_state(state)

    # 4) 画像・原本。消したものを先に (30日で取れなくなるので)
    pending = []
    for oid, st in objs.items():
        for kind, info in (st.get("media") or {}).items():
            if info.get("status") == "pending":
                pending.append((0 if st.get("deleted") else 1, _neg(st.get("created")), oid, kind))
    pending.sort()
    if not args.no_media:
        done = 0
        for _, _, oid, kind in pending:
            if args.max_media and done >= args.max_media:
                break
            if not client.can_spend():
                note.append("30日枠の残りが少ないので、画像・原本はここまで。続きは次回")
                break
            st = objs[oid]
            info = st["media"][kind]
            obj = load_json(raw_path(oid)) or {}
            try:
                client.wait_for_burst()
                fetch_media(client, oid, kind, info, obj, st, cfg)
                stats["media"] += 1
                done += 1
                write_if_changed(md_path(st), build_markdown(obj, st, spaces_map))
            except StopForQuota:
                note.append("画像・原本の途中でクレジットが尽きました。続きは次回")
                break
            except AuthError as e:
                note.append(str(e))
                break
            except ApiError as e:
                if e.status == 422:
                    info["status"] = "none"
                elif e.status == 404:
                    info["status"] = "gone"
                elif e.status == 413:
                    info["status"] = "too_large"
                else:
                    info["tries"] = int(info.get("tries") or 0) + 1
                    info["error"] = str(e)[:200]
                    if info["tries"] >= 5:
                        info["status"] = "failed"
            except OSError as e:
                info["tries"] = int(info.get("tries") or 0) + 1
                info["error"] = str(e)[:200]
            if done and done % 20 == 0:
                save_state(state)
    stats["media_pending"] = sum(
        1 for st in objs.values() for i in (st.get("media") or {}).values() if i.get("status") == "pending")

    # 5) 一覧ファイル、実行記録、写し
    write_index(state)
    state["last_run"] = now_iso
    save_state(state)
    summary = dict({k: v for k, v in stats.items() if k != "mirrored"}, at=now_iso, listed=len(seen), complete=complete, total=len(objs),
                   deleted_total=sum(1 for s in objs.values() if s.get("deleted")),
                   cost=client.cost, requests=client.requests,
                   quota={k: v for k, v in client.limits.items()}, note=note)
    with open(os.path.join(DATA, "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    if cfg["mirror_dir"]:
        parent = os.path.dirname(cfg["mirror_dir"].rstrip("/"))
        if os.path.isdir(parent):
            stats["mirrored"] = mirror(DATA, cfg["mirror_dir"])
        else:
            note.append("写し先の親フォルダが見つかりません: %s" % parent)
    for n in note:
        log("注意: " + n)
    log("終了 削除 %d / 画像・原本 %d 取得 (残り %d) / 写し %d ファイル / 消費 %s  %s" % (
        stats["deleted"], stats["media"], stats["media_pending"], stats["mirrored"],
        client.cost, client.quota_text()))
    lock.close()
    return 0 if complete else 1


def _neg(iso):
    dt = parse_iso(iso)
    return -dt.timestamp() if dt else 0.0


def probe():
    """何も書かずに、認証・件数・消費クレジットを確かめる。"""
    cfg = load_config()
    set_data_dir(cfg)
    kid, secret = load_credentials()
    state = {}
    client = Client(kid, secret, state, cfg)
    print("API: " + API_BASE)
    try:
        client.get_json("/objects", {"limit": "1"})
    except AuthError as e:
        print("NG  " + str(e))
        return 2
    except ApiError as e:
        print("NG  " + str(e))
        return 1
    decode, with_query = AUTH_MODES[client.mode]
    print("OK  認証が通りました (シークレット: %s / path: %s)" % (
        "base64デコードして使用" if decode else "文字列のまま使用", "クエリ込み" if with_query else "パスのみ"))
    print("    1件取得の消費: %s   %s" % (client.last_cost, client.quota_text()))

    n = with_blob = with_shot = deleted = 0
    first = sample = None
    before = client.cost
    for obj in client.iter_objects({"contentAs": "text/markdown"}):
        n += 1
        first = first or obj
        if obj.get("blob"):
            with_blob += 1
        if obj.get("screenshot"):
            with_shot += 1
            sample = sample or obj
        if obj.get("deleted"):
            deleted += 1
    print("OK  一覧: %d 件 (原本ファイルあり %d / スクリーンショットあり %d / 削除済み %d)" % (
        n, with_blob, with_shot, deleted))
    print("    一覧 1 回の消費: %s   %s" % (client.cost - before, client.quota_text()))
    if first:
        print("    項目: " + ", ".join(sorted(first.keys())))
    if sample:
        tmp = os.path.join(tempfile.mkdtemp(prefix="mymind-probe-"), "shot")
        before = client.cost
        meta = sample["screenshot"] if isinstance(sample["screenshot"], dict) else {}
        try:
            how = "API経由"
            if meta.get("url"):
                try:
                    client.fetch_plain(meta["url"], tmp)
                    how = "URL直接 (クレジット消費なし)"
                except ApiError:
                    client.download("/objects/%s/screenshot" % sample["id"], tmp)
            else:
                client.download("/objects/%s/screenshot" % sample["id"], tmp)
            print("OK  スクリーンショット 1 枚: %s, %d KB, 消費 %s" % (
                how, os.path.getsize(tmp) // 1024, client.cost - before))
            per = max(client.cost - before, 0)
            print("    見積もり: 画像・原本を全部取ると およそ %d クレジット" % (per * (with_blob + with_shot)))
        except (ApiError, StopForQuota) as e:
            print("NG  スクリーンショットが取れませんでした: %s" % e)
        finally:
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    print("    データ置き場: %s" % cfg["data_dir"])
    print("    写し先: %s" % (cfg["mirror_dir"] or "未設定"))
    return 0


def main():
    p = argparse.ArgumentParser(description="mymind の中身を手元に溜めていく")
    p.add_argument("--probe", action="store_true", help="認証と件数・消費クレジットを確かめるだけ (何も書かない)")
    p.add_argument("--no-media", action="store_true", help="画像・原本を取りに行かない")
    p.add_argument("--max-media", type=int, default=0, help="今回取る画像・原本の上限")
    p.add_argument("--accept-mass-delete", action="store_true", help="大量に消えていても削除として記録する")
    p.add_argument("--rebuild", action="store_true", help="手元の生データから Markdown を作り直す (通信なし)")
    args = p.parse_args()
    if args.probe:
        return probe()
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
