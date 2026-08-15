#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""猜猜器 · Guess Lab 后端（Phase 2，零第三方依赖，仅 Python 标准库）

职责：
  1. 行为事件流落库（SQLite）：swipe / loop / view，含时间戳与详情
  2. 服务端 feed 引擎：难度档（由行为推断）、卡点词汇记忆、换语境复现
  3. 图文阅读区文本重组：只复用"用户亲耳听过"的词汇（确定性回退 + 可选 LLM）
  4. 静态托管 App 本体（index.html / scenes.js ...）

LLM 可插拔：设置环境变量 DEEPSEEK_API_KEY 后，feed 重排与阅读区重组走
DeepSeek API；未设置时使用确定性规则（本仓库默认路线，零成本）。

用法：
  python3 server.py [--port 8787] [--data ./data]
"""
import argparse, json, os, re, sqlite3, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 词汇工具
FUNC_WORDS = set("""i you he she it we they me him her us them my your his its our their
is are am was were be been being do does did can could will would shall should may might must
have has had get got the a an to in on at by for with without of from up down out here there
so this that these those and or but not no yes oh ah ahh uh uhh mm mmm yum crunch tweet drip
drop z hey ok okay very really just only too also then than now again some all any more most
much many one two three let wanna gonna go come take give put keep make want need like love
look see say said know think feel tell ask show try help work play stay live move turn start
stop good bad big small long short new old pretty nice great best well thing things way right
wrong sure true what where who when why how day night today please thanks thank bye yeah hmm
um coz cause cos gotta kinda sorta""".split())

CONTR = {"it's":"it","lets":"let","let's":"let","i'm":"i","you're":"you","don't":"do",
         "doesn't":"does","didn't":"did","can't":"can","won't":"will","isn't":"is",
         "aren't":"are","i'll":"i","you'll":"you","we'll":"we","i've":"i","you've":"you",
         "we've":"we","there's":"there"}
IRREG = {"dug":"dig","flew":"fly","ate":"eat","slept":"sleep","ran":"run","drank":"drink",
         "threw":"throw","caught":"catch","made":"make","went":"go","sang":"sing",
         "broke":"break"}

def stem(w):
    w = w.lower()
    if w in CONTR: w = CONTR[w]
    if w in IRREG: return IRREG[w]
    if w.endswith("ies"): return w[:-3] + "y"
    if w.endswith("ing"):
        b = w[:-3]
        if len(b) >= 2 and b[-1] == b[-2]: b = b[:-1]
        return b
    if w.endswith("es"): return w[:-2]
    if w.endswith("s") and not w.endswith("ss"): return w[:-1]
    if w.endswith("ed") and len(w) > 3: return w[:-2]
    return w

def content_words(line):
    out = []
    for raw in re.sub(r"[^a-z' ]", " ", line.lower()).split():
        if raw in FUNC_WORDS or stem(raw) in FUNC_WORDS:
            continue
        out.append(stem(raw))
    return out

# ---------------------------------------------------------------- 数据层
class Database:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()   # 单连接跨线程必须串行化
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY, created_at INTEGER)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, scene_id TEXT,
            action TEXT, ts INTEGER, detail TEXT)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_user ON events(user_id, ts)")
        self.conn.commit()

    def ensure_user(self, user_id):
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (user_id, int(time.time())))
            self.conn.commit()

    def add_event(self, user_id, scene_id, action, ts, detail):
        with self.lock:
            self.conn.execute("INSERT INTO events(user_id,scene_id,action,ts,detail) VALUES(?,?,?,?,?)",
                              (user_id, scene_id, action, ts, json.dumps(detail or {})))
            self.conn.commit()

    def events(self, user_id, action=None, limit=400):
        with self.lock:
            if action:
                cur = self.conn.execute(
                    "SELECT scene_id,action,ts,detail FROM events WHERE user_id=? AND action=? ORDER BY id DESC LIMIT ?",
                    (user_id, action, limit))
            else:
                cur = self.conn.execute(
                    "SELECT scene_id,action,ts,detail FROM events WHERE user_id=? ORDER BY id ASC LIMIT ?",
                    (user_id, limit))
            rows = cur.fetchall()
        return [{"scene_id": r[0], "action": r[1], "ts": r[2], "detail": json.loads(r[3] or "{}")}
                for r in rows]

# ---------------------------------------------------------------- 引擎
class Engine:
    """feed 引擎：难度推断 / 卡点记忆 / 换语境复现（与客户端规则一致，服务端权威版）"""

    def __init__(self, scenes_meta):
        self.scenes = {s["id"]: s for s in scenes_meta}
        self.by_tier = {}
        for s in scenes_meta:
            self.by_tier.setdefault(s["tier"], []).append(s["id"])

    def _swipe_events(self, db, user, n=40):
        return db.events(user, action="swipe_next", limit=n)

    def tier(self, db, user):
        sw = self._swipe_events(db, user)
        if len(sw) < 5:
            return 0
        recent = sw[:10]
        fast = [e for e in recent if (e["detail"] or {}).get("dtMs", 99999) < 4500]
        if not fast:
            return 0
        ratio = len(fast) / len(recent)
        if ratio <= 0.6:
            return 0
        seen = {e["scene_id"] for e in db.events(user, action="swipe_next", limit=20000)}
        # 逐档步进：升到 N 档要求 N-1 档已全部刷完（与客户端规则一致）
        t = 0
        if self.by_tier.get(0) and all(i in seen for i in self.by_tier[0]):
            t = 1
        if t >= 1 and ratio > 0.7 and self.by_tier.get(1) and all(i in seen for i in self.by_tier[1]):
            t = 2
        return t

    def memory_words(self, db, user):
        mem = {}
        for e in db.events(user, action="loop", limit=20000):
            cnt = (e["detail"] or {}).get("count", 1)
            if cnt >= 3:
                s = self.scenes.get(e["scene_id"])
                if s:
                    for w in s["vocab"]:
                        mem[w] = mem.get(w, 0) + 1
        return mem

    def order(self, db, user):
        return [e["scene_id"] for e in db.events(user, action="swipe_next", limit=20000)]

    def _level_ok(self, sc, level):
        if not level or level == "all":
            return True
        lv = sc.get("level", 3)
        if level == "1-2": return lv <= 2
        if level == "3": return lv == 3
        if level == "4-5": return lv >= 4
        return True

    def pick_next(self, db, user, lang=None, level=None):
        # 该级别下没有任何场景时，回退到全部级别，避免空池报错
        if not any(self._level_ok(s, level) for s in self.scenes.values()):
            level = None
        mem = self.memory_words(db, user)
        order = self.order(db, user)
        seen = set(order)
        t = self.tier(db, user)
        def pool(tier, lang, level):
            ids = [i for i in self.by_tier.get(tier, [])
                   if (not lang or (self.scenes[i].get("lang") or "en") == lang)
                   and self._level_ok(self.scenes[i], level)]
            return ids
        # 1) 换语境复现：有卡点词且每第 4 条
        if mem and len(order) >= 4 and len(order) % 4 == 0:
            cands = [s for s in self.scenes.values()
                     if any(v in mem for v in s["vocab"])
                     and (not lang or (s.get("lang") or "en") == lang)
                     and self._level_ok(s, level)]
            if cands:
                last_at = {e["scene_id"]: e["ts"] for e in db.events(user, action="swipe_next", limit=20000)}
                cands.sort(key=lambda s: last_at.get(s["id"], 0))
                return cands[0]["id"], "recontext:" + ",".join(w for w in mem if w in cands[0]["vocab"])
        # 2) 当前档内未见
        for tier in [t, t + 1, 2]:
            un = [i for i in pool(tier, lang, level) if i not in seen]
            if un:
                return un[0], "tier%d:unseen" % (tier + 1)
        # 3) 全部看过：最久未看
        last_at = {e["scene_id"]: e["ts"] for e in db.events(user, action="swipe_next", limit=20000)}
        pool_ids = []
        for tier in [0, 1, 2]:
            pool_ids += pool(tier, lang, level)
        oldest = sorted(pool_ids, key=lambda i: last_at.get(i, 0))
        return oldest[0], "review:oldest"

    # ---------------- 阅读区：确定性重组（零成本回退） ----------------
    def reading_cards(self, db, user, max_cards=6, lang=None, level=None):
        order = self.order(db, user)
        if not order:
            return []
        seen_ids = []
        for sid in order:
            if sid not in seen_ids:
                seen_ids.append(sid)
        watched = [self.scenes[sid] for sid in seen_ids
                   if sid in self.scenes and (not lang or (self.scenes[sid].get("lang") or "en") == lang)
                   and self._level_ok(self.scenes[sid], level)]
        if not watched:
            return []

        # 话题分组：统计看过词汇里共现最高的实词
        word_count = {}
        for s in watched:
            for w in s["vocab"]:
                word_count[w] = word_count.get(w, 0) + 1
        top = sorted(word_count.items(), key=lambda kv: -kv[1])[:3]
        cards = []
        used_lines = set()
        for w, _ in top:
            group = [s for s in watched if w in s["vocab"]]
            if not group or len(cards) >= max_cards:
                continue
            # 句子按"该句词汇在已看库里的强化次数"排序：听得最多的排前面
            def reinforce(s):
                return sum(word_count.get(v, 0) for v in s["vocab"])
            group.sort(key=reinforce, reverse=True)
            lines = []
            for s in group:
                for ln in s["lines"]:
                    key = (s["id"], ln)
                    if key in used_lines:
                        continue
                    used_lines.add(key)
                    lines.append(ln)
            cards.append({
                "id": "topic-" + w,
                "title": "All about " + w,
                "text": "\n".join(lines[:8]),
                "words": sorted({v for s in group for v in s["vocab"]}),
                "sourceSceneIds": [s["id"] for s in group],
                "mode": "rule"
            })
        return cards

# ---------------------------------------------------------------- LLM 提供器（可选）
class LLMProvider:
    """有 DEEPSEEK_API_KEY 时启用；无 key 时引擎走确定性规则。"""

    def __init__(self):
        self.key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.base = "https://api.deepseek.com/chat/completions"

    @property
    def enabled(self):
        return bool(self.key)

    def _call(self, system, user_prompt, temperature=0.7, max_tokens=400):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_prompt}],
            "temperature": temperature, "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(self.base, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"].strip()

    def recombine_reading(self, watched):
        """LLM 版阅读区：用'只看过'的词汇写新段落。词汇越界则回退确定性。"""
        vocab = sorted({v for s in watched for v in s["vocab"]})
        sentences = [ln for s in watched for ln in s["lines"]]
        allowed = set(vocab)
        prompt = (
            "Write ONE short passage (2-4 sentences, simple English for children).\n"
            "RULES: use ONLY words from this list: " + ", ".join(vocab) + "\n"
            "You may also use any basic function words (the, a, is, are, I, it, and, on, in, to, so, let's...).\n"
            "Do NOT use any other content words. No translation. No Chinese.\n"
            "Topic hint (sentences the learner already heard):\n" + " | ".join(sentences[:10]))
        try:
            text = self._call(
                "You help build comprehensible-input reading material for zero-beginners.",
                prompt, temperature=0.8, max_tokens=200)
            bad = []
            for raw in re.sub(r"[^a-z' ]", " ", text.lower()).split():
                if raw in FUNC_WORDS or stem(raw) in FUNC_WORDS:
                    continue
                if raw in allowed or stem(raw) in allowed:
                    continue
                bad.append(raw)
            if bad:
                return None, bad
            return text, None
        except Exception as e:
            return None, str(e)

# ---------------------------------------------------------------- HTTP
def json_ok(handler, payload, code=200):
    data = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode() or "{}")

class Handler(BaseHTTPRequestHandler):
    server_version = "GuessLab/0.1"

    def log_message(self, fmt, *args):  # 精简日志
        print("[%s] %s" % (self.address_string(), fmt % args))

    def _api(self):
        return self.path.startswith("/api/")

    def _guard(self, fn):
        try:
            return fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                return json_ok(self, {"error": "internal: " + str(e)}, 500)
            except Exception:
                pass

    def do_GET(self):
        self._guard(self._do_get)

    def do_POST(self):
        self._guard(self._do_post)

    def _do_get(self):
        if self.path == "/api/health":
            return json_ok(self, {"ok": True, "mode": "server",
                                  "llm": LLM.enabled, "scenes": len(ENGINE.scenes),
                                  "time": int(time.time())})
        m = re.match(r"^/api/reading\?userId=(.+?)(&lang=(.+?))?(&level=(.+))?$", self.path)
        if m:
            uid = m.group(1)
            lang = m.group(3) or None
            level = m.group(5) or None
            return json_ok(self, {"cards": self._reading(uid, lang, level)})
        m = re.match(r"^/api/user\?userId=(.+)$", self.path)
        if m:
            uid = m.group(1)
            return json_ok(self, self._profile(uid))
        if self.path.startswith("/api/"):
            return json_ok(self, {"error": "not found"}, 404)
        self._static()

    def _do_post(self):
        if self.path == "/api/users":
            body = read_json(self)
            uid = body.get("userId") or ("u_" + str(int(time.time() * 1000)))
            DB.ensure_user(uid)
            return json_ok(self, {"userId": uid, "createdAt": int(time.time())})
        if self.path == "/api/events":
            body = read_json(self)
            uid = body.get("userId")
            if not uid:
                return json_ok(self, {"error": "userId required"}, 400)
            DB.ensure_user(uid)
            DB.add_event(uid, body.get("sceneId", ""), body.get("action", ""),
                         body.get("ts") or int(time.time() * 1000), body.get("detail"))
            return json_ok(self, {"ok": True})
        if self.path == "/api/feed/next":
            body = read_json(self)
            uid = body.get("userId")
            if not uid:
                return json_ok(self, {"error": "userId required"}, 400)
            DB.ensure_user(uid)
            scene_id, reason = ENGINE.pick_next(DB, uid, body.get("lang"), body.get("level"))
            return json_ok(self, {"sceneId": scene_id, "reason": reason,
                                  "tier": ENGINE.tier(DB, uid) + 1})
        return json_ok(self, {"error": "not found"}, 404)

    # ---------- 业务 ----------
    def _reading(self, uid, lang=None, level=None):
        DB.ensure_user(uid)
        order = ENGINE.order(DB, uid)
        watched = [ENGINE.scenes[sid] for sid in order if sid in ENGINE.scenes]
        watched = [s for i, s in enumerate(watched) if s not in watched[:i]]
        watched = [s for s in watched if (not lang or (s.get("lang") or "en") == lang)
                   and ENGINE._level_ok(s, level)]
        if not watched:
            return []
        cards = ENGINE.reading_cards(DB, uid, lang=lang, level=level)
        if LLM.enabled and cards:
            text, err = LLM.recombine_reading(watched)
            if text:
                return [{"id": "llm-reading", "title": "All about what you heard",
                         "text": text,
                         "words": sorted({v for s in watched for v in s["vocab"]}),
                         "sourceSceneIds": [s["id"] for s in watched[:3]],
                         "mode": "llm"}]
        return cards

    def _profile(self, uid):
        DB.ensure_user(uid)
        order = ENGINE.order(DB, uid)
        mem = ENGINE.memory_words(DB, uid)
        sw = ENGINE._swipe_events(DB, uid, 10)
        fast = [e for e in sw if (e["detail"] or {}).get("dtMs", 99999) < 4500]
        return {
            "userId": uid,
            "tier": ENGINE.tier(DB, uid) + 1,
            "scenesSeen": len(set(order)),
            "totalSwipes": len(order),
            "memoryWords": mem,
            "recentFastSwipeRatio": ("%.0f%%" % (len(fast) / len(sw) * 100)) if sw else "—",
            "order": order[-20:]
        }

    # ---------- 静态 ----------
    def _static(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        if not rel:
            rel = "index.html"
        target = (BASE / rel).resolve()
        if not str(target).startswith(str(BASE)) or not target.is_file():
            return json_ok(self, {"error": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".webmanifest": "application/manifest+json; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml", ".mp4": "video/mp4", ".mp3": "audio/mpeg",
                 ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 缓存策略（解决海外节点卡顿：视频/海报内容不变，浏览器长缓存，第二次刷不再下载）
        if target.suffix in (".mp4", ".mp3", ".jpg", ".jpeg", ".png", ".svg", ".webmanifest"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif target.suffix == ".js" and target.name == "scenes.st.js":
            self.send_header("Cache-Control", "public, max-age=300")  # 内容更新时靠 ?v= 版本号
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

# ---------------------------------------------------------------- 启动
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--data", default=str(BASE / "data"))
    args = ap.parse_args()
    Path(args.data).mkdir(parents=True, exist_ok=True)

    meta = json.loads((BASE / "scenes.meta.json").read_text())
    global DB, ENGINE, LLM
    DB = Database(Path(args.data) / "guesslab.db")
    ENGINE = Engine(meta["scenes"])
    LLM = LLMProvider()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("猜猜器后端已启动: http://0.0.0.0:%d  (LLM: %s, 场景: %d)" %
          (args.port, "DeepSeek" if LLM.enabled else "规则引擎(零成本)", len(ENGINE.scenes)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

# 模块级占位（main 里替换为实例）
DB = None
ENGINE = None
LLM = None

if __name__ == "__main__":
    main()
