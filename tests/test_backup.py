# -*- coding: utf-8 -*-
"""backup.py を、作り物の API サーバー相手に通しで動かすテスト。

    python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_api import KID, SECRET_B64, MockMymind  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(HERE), "backup.py")
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 10
PDF = b"%PDF-1.4 fake pdf " * 10


def oid(n):
    return "%022d" % n


def make_obj(n, title, **extra):
    obj = {
        "id": oid(n),
        "title": title,
        "summary": "summary of %d" % n,
        "tags": [{"name": "ai-tag", "flags": 2}, {"name": "mine", "flags": 8}],
        "spaces": [{"id": "SPACE00000000000000001"}],
        "notes": [],
        "created": "2026-01-0%dT03:00:00.123456Z" % n,
        "modified": "2026-01-0%dT03:00:00.123456Z" % n,
        "bumped": "2026-01-0%dT03:00:00.123456Z" % n,
    }
    obj.update(extra)
    return obj


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.mirror = os.path.join(self.tmp.name, "cloud", "mymind-archive")
        self.config = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.dirname(self.mirror))
        os.makedirs(self.config)
        self.mock = MockMymind()
        m = self.mock
        m.objects[oid(1)] = make_obj(
            1, "Article: with / slash? and #hash",
            source={"url": "https://example.com/article"},
            mainEntity={"@type": "Article"},
            content={"type": "text/markdown", "body": "# Heading\n\nArticle body."},
            screenshot={"path": "shots/1.jpg", "type": "image/jpeg"},
        )
        m.files[(oid(1), "screenshot")] = (JPEG, "image/jpeg")
        m.objects[oid(2)] = make_obj(
            2, "日本語のメモ",
            mainEntity={"@type": "Note"},
            content={"type": "text/markdown", "body": "メモの本文"},
            notes=[{"id": "n1", "content": {"type": "application/prose+json", "body": {
                "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "prose note"}]}]}}}],
        )
        m.objects[oid(3)] = make_obj(
            3, "Uploaded PDF", mainEntity={"@type": "Document"},
            blob={"path": "blobs/3.pdf", "type": "application/pdf", "name": "paper.pdf"},
        )
        m.files[(oid(3), "blob")] = (PDF, "application/pdf")
        m.objects[oid(4)] = make_obj(
            4, "Uploaded image", mainEntity={"@type": "Image"},
            blob={"path": "blobs/4.jpg", "type": "image/jpeg"},
        )
        m.files[(oid(4), "blob")] = (JPEG, "image/jpeg")
        m.objects[oid(5)] = make_obj(5, "Plain bookmark", source={"url": "https://example.com/b"})

    def tearDown(self):
        self.mock.close()
        self.tmp.cleanup()

    # --- 道具
    def run_backup(self, *args, secret=SECRET_B64, mirror=True, expect=0):
        env = dict(os.environ)
        env.update({
            "MYMIND_API_BASE": self.mock.base,
            "MYMIND_KID": KID,
            "MYMIND_SECRET": secret,
            "MYMIND_ARCHIVE_DIR": self.data,
            "MYMIND_ARCHIVE_CONFIG_DIR": self.config,
            "PYTHONUTF8": "1",
        })
        env.pop("MYMIND_MIRROR_DIR", None)
        if mirror:
            env["MYMIND_MIRROR_DIR"] = self.mirror
        p = subprocess.run([sys.executable, SCRIPT] + list(args), env=env, capture_output=True, text=True, timeout=120)
        if expect is not None:
            self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p

    def state(self):
        with open(os.path.join(self.data, "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def item(self, n, suffix=".md"):
        st = self.state()["objects"][oid(n)]
        return os.path.join(self.data, "items", st["dir"], st["base"] + suffix)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def read_bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    # --- 通しのシナリオ
    def test_full_lifecycle(self):
        # 1回目: 全部取れる
        self.run_backup()
        st = self.state()
        self.assertEqual(len(st["objects"]), 5)
        md1 = self.read(self.item(1))
        self.assertIn('title: "Article: with / slash? and #hash"', md1)
        self.assertIn('url: "https://example.com/article"', md1)
        self.assertIn('spaces: ["Reading"]', md1)
        self.assertIn('tags_manual: ["mine"]', md1)
        self.assertIn("Article body.", md1)
        self.assertIn("status: active", md1)
        self.assertIn(".screenshot.jpg>)", md1)
        base1 = os.path.basename(self.item(1))
        self.assertTrue(base1.startswith("20260101_Article with slash and hash_"), base1)
        self.assertNotIn("/", base1)
        self.assertEqual(self.read_bytes(self.item(1, ".screenshot.jpg")), JPEG)
        self.assertEqual(self.read_bytes(self.item(3, ".pdf")), PDF)
        self.assertEqual(self.read_bytes(self.item(4, ".jpg")), JPEG)
        self.assertIn("prose note", self.read(self.item(2)))
        self.assertIn("メモの本文", self.read(self.item(2)))
        self.assertIn("原本ファイル: [", self.read(self.item(3)))
        # 生データはベクトル抜き、ベクトルは別ファイル
        raw = json.loads(self.read(os.path.join(self.data, "raw", oid(1) + ".json")))
        self.assertNotIn("embeddings", raw)
        self.assertEqual(len(json.loads(self.read(os.path.join(self.data, "raw", oid(1) + ".embeddings.json")))), 8)
        # CDN へのリダイレクトには認証ヘッダーを付けていない (付けたら 400 でスクショが取れない)
        self.assertEqual(st["objects"][oid(1)]["media"]["screenshot"]["status"], "done")
        # 写し
        self.assertTrue(os.path.exists(os.path.join(self.mirror, "index.jsonl")))
        self.assertTrue(os.path.exists(self.item(1).replace(self.data, self.mirror)))
        index = [json.loads(l) for l in self.read(os.path.join(self.data, "index.jsonl")).splitlines()]
        self.assertEqual(len(index), 5)

        # 2回目: 変化なし。何も書き直さず、画像も取り直さない
        mtime = os.path.getmtime(self.item(1))
        before = len(self.mock.requests)
        p = self.run_backup()
        self.assertIn("新規 0 / 更新 0 / 変化なし 5", p.stdout)
        self.assertEqual(os.path.getmtime(self.item(1)), mtime)
        self.assertEqual(len(self.mock.requests) - before, 2)  # spaces と一覧だけ

        # 更新: タイトルとメモが変わる。ファイル名は変えず、中身だけ最新に
        path_before = self.item(2)
        self.mock.objects[oid(2)]["title"] = "日本語のメモ (改)"
        self.mock.objects[oid(2)]["content"]["body"] = "書き直した本文"
        self.mock.objects[oid(2)]["modified"] = "2026-01-20T00:00:00Z"
        p = self.run_backup()
        self.assertIn("更新 1", p.stdout)
        self.assertEqual(self.item(2), path_before)
        md2 = self.read(self.item(2))
        self.assertIn("書き直した本文", md2)
        self.assertIn('title: "日本語のメモ (改)"', md2)
        self.assertNotIn("メモの本文", md2)

        # 削除 (ゴミ箱にある): 最後の状態を取りに行き、削除日を記録して残す
        self.mock.objects[oid(1)]["title"] = "Article (edited right before delete)"
        self.mock.soft_delete(oid(1), "2026-02-01T10:00:00Z")
        # 完全に消えたもの (ゴミ箱にもない)
        self.mock.purge(oid(5))
        p = self.run_backup()
        self.assertIn("削除 2", p.stdout)
        st = self.state()["objects"]
        self.assertEqual(st[oid(1)]["deleted"], "2026-02-01T10:00:00Z")
        self.assertEqual(st[oid(1)]["deleted_how"], "api")
        self.assertEqual(st[oid(5)]["deleted_how"], "detected")
        md1 = self.read(self.item(1))
        self.assertIn("status: deleted", md1)
        self.assertIn("deleted: 2026-02-01T10:00:00Z", md1)
        self.assertIn("edited right before delete", md1)
        self.assertIn("Article body.", md1)
        self.assertTrue(os.path.exists(self.item(1, ".screenshot.jpg")))
        self.assertTrue(os.path.exists(self.item(5)))
        deleted_md = self.read(os.path.join(self.data, "deleted.md"))
        self.assertIn("2件", deleted_md)
        self.assertIn("edited right before delete", deleted_md)

        # 次の回: 削除済みを毎回確認しに行かない
        before = len(self.mock.requests)
        p = self.run_backup()
        self.assertIn("削除 0", p.stdout)
        self.assertEqual(len(self.mock.requests) - before, 2)

        # 復活: ゴミ箱から戻したら削除の印を外す
        self.mock.restore(oid(1))
        p = self.run_backup()
        self.assertIn("復活 1", p.stdout)
        self.assertIsNone(self.state()["objects"][oid(1)]["deleted"])
        self.assertIn("status: active", self.read(self.item(1)))
        self.assertNotIn("edited right before delete", self.read(os.path.join(self.data, "deleted.md")))

        # 写し側で消されても、次の回に戻る
        mirrored = self.item(3, ".pdf").replace(self.data, self.mirror)
        os.remove(mirrored)
        self.run_backup()
        self.assertTrue(os.path.exists(mirrored))

        # 実行記録
        runs = [json.loads(l) for l in self.read(os.path.join(self.data, "runs.jsonl")).splitlines()]
        self.assertEqual(len(runs), 7)
        self.assertTrue(all(r["complete"] for r in runs))

    def test_quota_stop_and_resume(self):
        # 30日枠の残りが少ない: 文章は全部取れて、画像・原本は次回に回る
        self.mock.sustained["r"] = 20000  # 100000 の 30% 未満
        p = self.run_backup()
        self.assertIn("画像・原本はここまで", p.stdout)
        st = self.state()["objects"]
        self.assertEqual(len(st), 5)
        self.assertEqual(st[oid(1)]["media"]["screenshot"]["status"], "pending")
        self.assertTrue(os.path.exists(self.item(1)))
        self.assertFalse(os.path.exists(self.item(1, ".screenshot.jpg")))
        # 枠が戻ったら続きから
        self.mock.sustained["r"] = 100000
        p = self.run_backup()
        self.assertIn("画像・原本 3 取得 (残り 0)", p.stdout)
        self.assertTrue(os.path.exists(self.item(1, ".screenshot.jpg")))
        self.assertIn(".screenshot.jpg>)", self.read(self.item(1)))

    def test_quota_exhausted_429(self):
        self.run_backup("--no-media")
        # 余裕を残す設定を切って、本当に 429 が返るところまで使い切らせる
        with open(os.path.join(self.config, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"sustained_reserve_ratio": 0}, f)
        self.mock.sustained["r"] = 12  # spaces(1) + 一覧(10) は通るが、その先で尽きる
        p = self.run_backup()
        self.assertIn("クレジットが尽きました", p.stdout)
        self.assertEqual(self.state()["objects"][oid(3)]["media"]["blob"]["status"], "pending")

    def test_auth_probing_raw_secret_and_query_path(self):
        self.mock.key_mode = "raw"
        self.mock.path_with_query = True
        p = self.run_backup("--probe")
        self.assertIn("認証が通りました", p.stdout)
        self.assertIn("文字列のまま使用", p.stdout)
        self.assertIn("クエリ込み", p.stdout)
        self.assertFalse(os.path.exists(self.data))  # お試しは何も書かない
        self.run_backup()
        self.assertEqual(self.state()["auth_mode"], 3)
        self.assertEqual(len(self.state()["objects"]), 5)

    def test_bad_secret(self):
        p = self.run_backup(secret="d3Jvbmctc2VjcmV0LXdyb25nLXNlY3JldC13cm9uZyE=", expect=2)
        self.assertIn("認証に失敗", p.stdout)

    def test_mass_delete_guard(self):
        self.run_backup("--no-media")
        saved = dict(self.mock.objects)
        self.mock.objects.clear()  # API の不調で一覧が空になった、という想定
        for n in range(6, 40):     # 守りが効く件数にするため、先に数を増やしておく
            saved[oid(n)] = make_obj(n % 9 + 1, "bulk %d" % n, id=oid(n))
        self.mock.objects.update(saved)
        self.run_backup("--no-media")
        self.mock.objects.clear()
        p = self.run_backup("--no-media")
        self.assertIn("多すぎるので今回は削除扱いにしていません", p.stdout)
        self.assertTrue(all(not s.get("deleted") for s in self.state()["objects"].values()))
        p = self.run_backup("--no-media", "--accept-mass-delete")
        self.assertTrue(all(s.get("deleted") for s in self.state()["objects"].values()))

    def test_truncated_list_does_not_mark_deletions(self):
        self.run_backup("--no-media")
        self.mock.truncate_list_after = 2
        p = self.run_backup("--no-media", expect=1)
        self.assertIn("一覧の取得が途中で失敗", p.stdout)
        self.assertTrue(all(not s.get("deleted") for s in self.state()["objects"].values()))

    def test_rebuild_and_data_dir_guard(self):
        self.run_backup()
        os.remove(self.item(2))
        p = self.run_backup("--rebuild")
        self.assertIn("作り直しました", p.stdout)
        self.assertTrue(os.path.exists(self.item(2)))
        # データ置き場がコードのリポジトリの中なら止まる (.git があるときだけ働く)
        repo = os.path.dirname(HERE)
        if os.path.exists(os.path.join(repo, ".git")):
            env = dict(os.environ, MYMIND_API_BASE=self.mock.base, MYMIND_KID=KID, MYMIND_SECRET=SECRET_B64,
                       MYMIND_ARCHIVE_DIR=os.path.join(repo, "data"), MYMIND_ARCHIVE_CONFIG_DIR=self.config)
            p = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("リポジトリの中", p.stderr)
            self.assertFalse(os.path.exists(os.path.join(repo, "data")))


if __name__ == "__main__":
    unittest.main()
