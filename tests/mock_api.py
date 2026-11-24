# -*- coding: utf-8 -*-
"""テスト用の、mymind API のまねをする小さなサーバー。中身はすべて作り物。"""

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KID = "test-kid"
SECRET_RAW = b"0123456789abcdef0123456789abcdef"
SECRET_B64 = base64.b64encode(SECRET_RAW).decode("ascii")


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class MockMymind:
    def __init__(self):
        self.objects = {}   # id -> object (生きているもの)
        self.trash = {}     # id -> object ("deleted" 付き)
        self.files = {}     # (id, kind) -> (bytes, content-type)
        self.spaces = [{"id": "SPACE00000000000000001", "name": "Reading"}]
        self.key_mode = "decoded"       # "decoded" か "raw"
        self.path_with_query = False
        self.burst = {"q": 10000, "w": 300, "r": 10000}
        self.sustained = {"q": 100000, "w": 2592000, "r": 100000}
        self.costs = {"list": 10, "get": 1, "media": 5, "spaces": 1}
        self.requests = []              # (method, path)
        self.cdn_hits = 0
        self.truncate_list_after = None  # 一覧を途中で切る (壊れた通信のまね)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # --- テストから操作する
    def soft_delete(self, oid, when="2026-02-01T00:00:00Z"):
        obj = self.objects.pop(oid)
        obj["deleted"] = when
        self.trash[oid] = obj

    def restore(self, oid):
        obj = self.trash.pop(oid)
        obj.pop("deleted", None)
        self.objects[oid] = obj

    def purge(self, oid):
        self.objects.pop(oid, None)
        self.trash.pop(oid, None)

    # --- 中身
    def _check_auth(self, handler, path, query):
        auth = handler.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        try:
            h, p, sig = auth[7:].split(".")
            header = json.loads(_b64url_decode(h))
            payload = json.loads(_b64url_decode(p))
        except Exception:
            return False
        if header.get("alg") != "HS256" or header.get("kid") != KID:
            return False
        key = SECRET_RAW if self.key_mode == "decoded" else SECRET_B64.encode("ascii")
        want = hmac.new(key, (h + "." + p).encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _b64url_decode(sig)):
            return False
        expect_path = path + ("?" + query if (self.path_with_query and query) else "")
        if payload.get("path") != expect_path or payload.get("method") != handler.command:
            return False
        return payload.get("exp", 0) >= time.time()

    def _rate_headers(self, cost):
        return {
            "RateLimit-Policy": '"burst";q=%d;w=%d, "sustained";q=%d;w=%d' % (
                self.burst["q"], self.burst["w"], self.sustained["q"], self.sustained["w"]),
            "RateLimit": '"burst";r=%d;t=1, "sustained";r=%d;t=2589945' % (
                self.burst["r"], self.sustained["r"]),
            "RateLimit-Cost": str(cost),
        }

    def _make_handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/json", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _problem(self, status, typ, detail, headers=None):
                body = json.dumps({"type": typ, "status": status, "detail": detail}).encode()
                self._send(status, body, "application/problem+json", headers)

            def do_GET(self):
                u = urlparse(self.path)
                path, query = u.path, u.query
                q = parse_qs(query)
                mock.requests.append((self.command, path))

                # CDN のまね。認証ヘッダーが付いていたら拒否する (署名付き URL と同じ振る舞い)
                if path.startswith("/cdn/"):
                    mock.cdn_hits += 1
                    if self.headers.get("Authorization"):
                        return self._send(400, b"auth header not allowed", "text/plain")
                    _, _, oid, kind = path.split("/")
                    f = mock.files.get((oid, kind))
                    if not f:
                        return self._send(404, b"", "text/plain")
                    return self._send(200, f[0], f[1])

                if not mock._check_auth(self, path, query):
                    return self._problem(401, "Unauthorized", "bad token")

                parts = [p for p in path.split("/") if p]
                kind = "get"
                if parts == ["objects"]:
                    kind = "list"
                elif parts == ["spaces"]:
                    kind = "spaces"
                elif len(parts) == 3:
                    kind = "media"
                cost = mock.costs[kind]
                if mock.sustained["r"] < cost or mock.burst["r"] < cost:
                    return self._problem(429, "RateLimited", "quota exhausted", mock._rate_headers(0))
                mock.sustained["r"] -= cost
                mock.burst["r"] -= cost
                rl = mock._rate_headers(cost)

                if parts == ["spaces"]:
                    return self._send(200, json.dumps(mock.spaces).encode(), headers=rl)

                if parts == ["objects"]:
                    objs = []
                    for o in mock.objects.values():
                        o = dict(o)
                        if q.get("include") == ["embeddings"]:
                            o["embeddings"] = [0.125] * 8
                        objs.append(o)
                    if "limit" in q:
                        objs = objs[: int(q["limit"][0])]
                    if "jsonl" in (self.headers.get("Accept") or ""):
                        if mock.truncate_list_after is not None:
                            # Content-Length より短く返して接続を切る
                            body = "".join(json.dumps(o) + "\n" for o in objs).encode()
                            self.send_response(200)
                            for k, v in rl.items():
                                self.send_header(k, v)
                            self.send_header("Content-Type", "application/jsonl")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            cut = "".join(json.dumps(o) + "\n" for o in objs[: mock.truncate_list_after]).encode()
                            self.wfile.write(cut)
                            self.wfile.flush()
                            self.close_connection = True
                            return
                        body = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs).encode("utf-8")
                        return self._send(200, body, "application/jsonl", rl)
                    return self._send(200, json.dumps(objs).encode(), headers=rl)

                if len(parts) >= 2 and parts[0] == "objects":
                    oid = parts[1]
                    obj = mock.objects.get(oid) or mock.trash.get(oid)
                    if obj is None:
                        return self._problem(404, "NotFound", "No object with that ID exists.", rl)
                    if len(parts) == 2:
                        return self._send(200, json.dumps(obj).encode(), headers=rl)
                    f = mock.files.get((oid, parts[2]))
                    if not f:
                        return self._problem(422, "Unprocessable", "no such media", rl)
                    if parts[2] == "screenshot":
                        # 相対 URL でリダイレクト
                        return self._send(302, b"", headers=dict(rl, Location="/cdn/%s/%s" % (oid, parts[2])))
                    return self._send(200, f[0], f[1], rl)

                return self._problem(404, "NotFound", "unknown path", rl)

        return Handler
