import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import feedparser
import json
import os
import sys
import asyncio
import secrets
import re
import requests
import time
import html
from flask import Flask, render_template_string, request, redirect, session, url_for
from threading import Thread

# 讀取 .env 檔案中的環境變數
load_dotenv()

# --- [設定與資料處理] ---
TOKEN = os.getenv("DISCORD_TOKEN") # 🌸 從 .env 檔案讀取 Discord Bot Token

DATA_FOLDER = 'guild_data'
KEY_FILE = 'web_keys.json'
if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)

def load_keys():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    return {}

def save_keys(keys):
    with open(KEY_FILE, 'w', encoding='utf-8') as f:
        json.dump(keys, f, indent=4)

def load_guild_data(guild_id):
    path = os.path.join(DATA_FOLDER, f"{guild_id}.json")
    # 預設資料結構
    default_data = {
        "yt": [], 
        "channel_id": None, 
        "format": "&e &who 發布了新影片：&url", 
        "guild_name": "未知伺服器"
    }
    
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            try:
                d = json.load(f)
                # 自動補齊缺失的欄位
                for key, value in default_data.items():
                    if key not in d:
                        d[key] = value
                return d
            except json.JSONDecodeError:
                return default_data
    return default_data

def save_guild_data(guild_id, data):
    with open(os.path.join(DATA_FOLDER, f"{guild_id}.json"), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def translate_message(fmt, who, url, title):
    return fmt.replace("&e", "@everyone").replace("&who", who).replace("&url", url).replace("&str", title)

# --- [YouTube 抓取引擎：Shorts 強化版] ---
def fetch_latest_video(channel_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-TW,zh;q=0.9'
    }
    
    candidates = []

    # 1. 嘗試 RSS (一般影片最快)
    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        r = requests.get(rss_url, headers=headers, timeout=10)
        if r.status_code == 200:
            feed = feedparser.parse(r.text)
            if feed.entries:
                e = feed.entries[0]
                candidates.append({
                    "title": e.title,
                    "link": e.link,
                    "thumb": e.media_thumbnail[0]['url'] if 'media_thumbnail' in e else None,
                    "published": time.mktime(e.published_parsed) if 'published_parsed' in e else 0
                })
    except Exception as e:
        print(f"[ERROR] RSS fetch failed for {channel_id}: {e}", file=sys.stderr)

    # 2. 嘗試爬取 /shorts 頁面
    try:
        r_shorts = requests.get(f"https://www.youtube.com/channel/{channel_id}/shorts", headers=headers, timeout=10)
        s_match = re.search(r'"videoId":"([^"]+)"', r_shorts.text)
        t_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"', r_shorts.text)
        if s_match:
            vid = s_match.group(1)
            candidates.append({
                "title": html.unescape(t_match.group(1)) if t_match else "最新 Shorts",
                "link": f"https://www.youtube.com/shorts/{vid}",
                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "published": time.time()
            })
    except Exception as e:
        print(f"[ERROR] Shorts page scrape failed for {channel_id}: {e}", file=sys.stderr)

    # 3. 備用：一般影片頁面
    try:
        r_vid = requests.get(f"https://www.youtube.com/channel/{channel_id}/videos", headers=headers, timeout=10)
        v_match = re.search(r'"videoId":"([^"]+)"', r_vid.text)
        if v_match:
            vid = v_match.group(1)
            candidates.append({
                "title": "最新影片內容",
                "link": f"https://www.youtube.com/watch?v={vid}",
                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "published": time.time() - 60
            })
    except Exception as e:
        print(f"[ERROR] Videos page scrape failed for {channel_id}: {e}", file=sys.stderr)

    if candidates:
        return max(candidates, key=lambda x: x['published'])
    return None

def verify_yt(handle_or_id):
    handle = handle_or_id.replace("@", "").strip()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'}
    try:
        channel_id = handle if handle.startswith('UC') and len(handle) == 24 else None
        name = handle
        if not channel_id:
            r = requests.get(f"https://www.youtube.com/@{handle}", headers=headers, timeout=10)
            p = [r'channel/(UC[a-zA-Z0-9_-]{22})', r'"externalId":"(UC[a-zA-Z0-9_-]{22})"', r'identifier" content="(UC[a-zA-Z0-9_-]{22})"']
            channel_id = next((re.search(pat, r.text).group(1) for pat in p if re.search(pat, r.text)), None)
            n_match = re.search(r'"name":"(.*?)"', r.text)
            if n_match: name = html.unescape(n_match.group(1).encode().decode('unicode_escape', 'ignore'))

        if not channel_id:
            return None, "找不到 ID"
        video = fetch_latest_video(channel_id)
        return {"id": channel_id, "name": name, "last_video": video}, None
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Network error during YouTube verification for {handle_or_id}: {e}", file=sys.stderr)
        return None, f"驗證失敗：網路錯誤"
    except Exception as e:
        print(f"[ERROR] Unexpected error during YouTube verification for {handle_or_id}: {e}", file=sys.stderr)
        return None, "驗證失敗：發生未知錯誤"

# --- [機器人邏輯] ---
class RuixueBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.last_links = {}

    async def setup_hook(self):
        pass

    async def on_ready(self):
        await self.tree.sync()
        print(f'🌸 機器人 {self.user} 登入成功！')
        if not self.check_loop.is_running():
            self.check_loop.start()

    @tasks.loop(minutes=5)
    async def check_loop(self):
        for filename in os.listdir(DATA_FOLDER):
            if not filename.endswith(".json"):
                continue
            gid = os.path.splitext(filename)[0]
            data = load_guild_data(gid)
            if not data.get("channel_id") or not data.get("yt"):
                continue
            
            discord_ch = self.get_channel(int(data["channel_id"]))
            if not discord_ch:
                continue
            
            if gid not in self.last_links: self.last_links[gid] = {}

            for yt in data["yt"]:
                video = fetch_latest_video(yt['id'])
                if video and (yt['id'] not in self.last_links[gid] or video['link'] != self.last_links[gid][yt['id']]):
                    self.last_links[gid][yt['id']] = video['link']
                    msg = translate_message(data["format"], yt["name"], video['link'], video['title'])
                    await discord_ch.send(msg)
                await asyncio.sleep(2)

bot = RuixueBot()

@bot.tree.command(name="git", description="申請管理密鑰")
async def git_key(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("只有管理員可以申請喔", ephemeral=True)
        return
    new_key = secrets.token_hex(8)
    keys = load_keys()
    keys[new_key] = str(interaction.guild_id)
    save_keys(keys)
    data = load_guild_data(interaction.guild_id)
    data["guild_name"] = interaction.guild.name
    save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message(f"密鑰已綁定！網頁登入請輸入：`{new_key}`", ephemeral=True)

@bot.tree.command(name="set_channel", description="設定目前的頻道為通知頻道")
async def set_ch(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("只有管理員可以使用此指令", ephemeral=True)
        return
    data = load_guild_data(interaction.guild_id)
    data["channel_id"] = interaction.channel_id
    save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message(f"✅ 通知頻道已設定為 <#{interaction.channel_id}>！")

@bot.tree.command(name="try", description="測試通知功能")
async def try_test(interaction: discord.Interaction):
    data = load_guild_data(interaction.guild_id)
    
    # 更嚴謹的判斷
    if not data.get("channel_id"):
        await interaction.response.send_message("❗ 尚未設定通知頻道，請先使用 `/set_channel`", ephemeral=True)
        return
    
    if not data.get("yt") or len(data["yt"]) == 0:
        await interaction.response.send_message("❗ 尚未新增追蹤的頻道，請透過網頁後台新增", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    try:
        # 抓取清單中第一個頻道進行測試
        video = fetch_latest_video(data["yt"][0]['id'])
        if video:
            msg = translate_message(data["format"], data["yt"][0]["name"], video['link'], video['title'])
            ch_id = int(data["channel_id"])
            ch = bot.get_channel(ch_id)
            if ch:
                await ch.send(f"🌸 **影片通知：**\n{msg}")
                await interaction.followup.send("💬 測試訊息已發出！請查看設定的頻道。")
            else:
                await interaction.followup.send("❌ 找不到通知頻道，請嘗試重新執行 `/set_channel`。")
        else:
            await interaction.followup.send("❌ 抓不到 YouTube 資料，可能是該頻道 ID 有誤。")
    except Exception as e:
        await interaction.followup.send(f"❌ 測試失敗：{str(e)}")

# --- [Flask 網頁介面] ---
app = Flask(__name__)
# 建議將 SECRET_KEY 存放在環境變數中，以確保 session 在重啟後依然有效
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pingall-ru</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #fff5f8; padding-top: 50px; font-family: 'Microsoft JhengHei', sans-serif; }
        .pink-card { border: none; border-radius: 20px; box-shadow: 0 10px 30px rgba(255,182,193,0.3); background: white; }
        .btn-pink { background: #ff85a2; color: white; border-radius: 20px; border: none; }
        .preview-box { background: #fff0f3; border-radius: 15px; border: 2px dashed #ff85a2; padding: 15px; margin-bottom: 20px; }
        .video-thumb { width: 100%; border-radius: 10px; margin-top: 10px; }
    </style>
</head>
<body>
<div class="container">
    <div class="row justify-content-center">
        <div class="col-12 col-md-6">
            {% if not session.gid %}
            <div class="card pink-card p-4 text-center">
                <h4 style="color:#ff85a2;">🔰 管理員登入</h4>
                <form action="/login" method="post"><input type="password" name="key" class="form-control mb-3 text-center rounded-pill" required><button type="submit" class="btn btn-pink w-100">管理伺服器</button></form>
            </div>
            {% else %}
            <div class="card pink-card p-4">
                <h5 class="text-center" style="color:#ff6b8d;">🌸 {{ g_name }}</h5>
                <hr>
                {% if preview %}
                <div class="preview-box">
                    <p class="text-center"><strong>{{ preview.name }}</strong></p>
                    {% if preview.last_video %}<p class="small text-center">{{ preview.last_video.title }}</p><img src="{{ preview.last_video.thumb }}" class="video-thumb">{% endif %}
                </div>
                {% endif %}
                <label class="small text-muted mb-1">通知內容格式：</label>
                <form action="/update_format" method="post" class="mb-4">
                    <div class="input-group"><input type="text" name="format" class="form-control" value="{{ current_format }}"><button type="submit" class="btn btn-outline-secondary">儲存</button></div>
                    <p class="small text-muted mt-1">變數：&e(@everyone), &who(名稱), &url(連結), &str(標題)</p>
                </form>
                <label class="small text-muted mb-1">新增 YouTube 追蹤：</label>
                <form action="/add" method="post" class="mb-4"><div class="input-group"><input type="text" name="yt_id" class="form-control" placeholder="例如：@YouTubeTaiwan"><button type="submit" class="btn btn-pink">新增</button></div></form>
                <div class="list-group">{% for yt in yt_list %}<div class="list-group-item d-flex justify-content-between align-items-center"><span>{{ yt.name }}</span><a href="/delete/{{ yt.id }}" class="btn btn-sm btn-danger">刪除</a></div>{% endfor %}</div>
                <div class="text-center mt-3"><a href="/logout" class="text-muted small">登出</a></div>
            </div>
            {% endif %}
        </div>
    </div>
</div>
</body>
</html>
'''

@app.route('/')
def index():
    gid = session.get('gid')
    if not gid:
        return render_template_string(HTML_TEMPLATE)
    data = load_guild_data(gid)
    preview = session.pop('preview_data', None)
    return render_template_string(HTML_TEMPLATE, g_name=data['guild_name'], yt_list=data['yt'], current_format=data['format'], preview=preview)

@app.route('/login', methods=['POST'])
def login():
    key = request.form.get('key')
    keys = load_keys()
    if key in keys:
        session['gid'] = keys[key]
    return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
def add():
    gid = session.get('gid')
    if not gid:
        return redirect(url_for('index'))
    info, _ = verify_yt(request.form.get('yt_id'))
    if info:
        data = load_guild_data(gid)
        if not any(i['id'] == info['id'] for i in data['yt']):
            data['yt'].append({"id": info['id'], "name": info['name']})
            save_guild_data(gid, data)
        session['preview_data'] = info
    return redirect(url_for('index'))

@app.route('/update_format', methods=['POST'])
def update_format():
    gid = session.get('gid')
    if gid:
        data = load_guild_data(gid)
        data['format'] = request.form.get('format')
        save_guild_data(gid, data)
    return redirect(url_for('index'))

# 警告：使用 GET 方法執行刪除操作有安全風險 (CSRF)。建議改用 POST 方法並加上確認步驟。
@app.route('/delete/<ytid>')
def delete(ytid):
    gid = session.get('gid')
    if gid:
        data = load_guild_data(gid)
        data['yt'] = [i for i in data['yt'] if i['id'] != ytid]
        save_guild_data(gid, data)
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

if __name__ == "__main__":
    if not TOKEN:
        print("❌ 錯誤：找不到 Discord 機器人 TOKEN。")
        print("💡 請在專案目錄下建立一個名為 .env 的檔案，並在其中加入一行：")
        print("   DISCORD_TOKEN=\"YOUR_DISCORD_TOKEN_HERE\"")
        exit(1)
    else:
        # 啟動 Flask 網頁
        Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)).start()
        # 啟動 Discord 機器人
        bot.run(TOKEN)
