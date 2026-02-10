import discord
from discord import app_commands
from discord.ext import tasks
import feedparser
import json
import os
import asyncio
import secrets
import re
import requests
import time
import html
from flask import Flask, render_template_string, request, redirect, session, url_for
from threading import Thread
from dotenv import load_dotenv

# 讀取環境變數
load_dotenv()

# --- [設定與資料處理] ---
TOKEN = os.getenv('DISCORD_TOKEN')
DATA_FOLDER = 'guild_data'
KEY_FILE = 'web_keys.json'

if not os.path.exists(DATA_FOLDER): os.makedirs(DATA_FOLDER)

def load_keys():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    return {}

def save_keys(keys):
    with open(KEY_FILE, 'w', encoding='utf-8') as f: json.dump(keys, f, indent=4)

def load_guild_data(guild_id):
    path = os.path.join(DATA_FOLDER, f"{guild_id}.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
            if "format" not in d: d["format"] = "&e &who 發布了新影片：&url"
            return d
    return {"yt": [], "channel_id": None, "format": "&e &who 發布了新影片：&url", "guild_name": "未知伺服器"}

def save_guild_data(guild_id, data):
    with open(os.path.join(DATA_FOLDER, f"{guild_id}.json"), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def translate_message(fmt, who, url, title):
    return fmt.replace("&e", "@everyone").replace("&who", who).replace("&url", url).replace("&str", title)

# --- [YouTube 抓取引擎：強化修正版] ---
def fetch_latest_video(channel_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-TW,zh;q=0.9'
    }
    # 嘗試 1: RSS Feed
    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}&v={int(time.time())}"
        r = requests.get(rss_url, headers=headers, timeout=10)
        if r.status_code == 200:
            feed = feedparser.parse(r.text)
            if feed.entries:
                entry = feed.entries[0]
                return {
                    "title": entry.title,
                    "link": entry.link,
                    "thumb": entry.media_thumbnail[0]['url'] if 'media_thumbnail' in entry else None
                }
    except: pass

    # 嘗試 2: 直接爬取影片頁面 (針對直播或 RSS 延遲)
    try:
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        r = requests.get(url, headers=headers, timeout=10)
        v_match = re.search(r'"videoId":"([^"]+)"', r.text)
        t_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"', r.text)
        if v_match:
            vid = v_match.group(1)
            title = t_match.group(1) if t_match else "最新內容"
            return {
                "title": html.unescape(title),
                "link": f"https://www.youtube.com/watch?v={vid}",
                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            }
    except: pass
    return None

def verify_yt(handle_or_id):
    handle = handle_or_id.replace("@", "").strip()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'}
    try:
        channel_id, name = None, handle
        if handle.startswith('UC') and len(handle) == 24:
            channel_id = handle
        else:
            r = requests.get(f"https://www.youtube.com/@{handle}", headers=headers, timeout=10)
            patterns = [
                r'https://www.youtube.com/channel/(UC[a-zA-Z0-9_-]{22})',
                r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
                r'meta itemprop="identifier" content="(UC[a-zA-Z0-9_-]{22})"'
            ]
            channel_id = next((re.search(p, r.text).group(1) for p in patterns if re.search(p, r.text)), None)
            n_match = re.search(r'"name":"(.*?)"', r.text)
            if n_match: name = html.unescape(n_match.group(1).encode().decode('unicode_escape', 'ignore'))

        if not channel_id: return None, "找不到頻道 ID"
        video = fetch_latest_video(channel_id)
        return {"id": channel_id, "name": name, "last_video": video}, None
    except: return None, "驗證失敗"

# --- [機器人邏輯] ---
class RuixueBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.last_links = {}

    async def setup_hook(self): self.check_loop.start()

    async def on_ready(self):
        await self.tree.sync()
        print(f'🌸 機器人 {self.user} 登入成功！')

    @tasks.loop(minutes=5)
    async def check_loop(self):
        for filename in os.listdir(DATA_FOLDER):
            if not filename.endswith(".json"): continue
            gid = filename.replace(".json", "")
            data = load_guild_data(gid)
            if not data.get("channel_id") or not data.get("yt"): continue
            channel = self.get_channel(int(data["channel_id"]))
            if not channel: continue
            if gid not in self.last_links: self.last_links[gid] = {}
            for yt in data["yt"]:
                video = fetch_latest_video(yt['id'])
                if video and (yt['id'] not in self.last_links[gid] or video['link'] != self.last_links[gid][yt['id']]):
                    self.last_links[gid][yt['id']] = video['link']
                    msg = translate_message(data["format"], yt["name"], video['link'], video['title'])
                    await channel.send(msg)
                await asyncio.sleep(1)

bot = RuixueBot()

# --- [Discord 指令] ---
@bot.tree.command(name="git", description="申請管理密鑰")
async def git_key(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("只有管理員可以申請喔", ephemeral=True)
        return
    new_key = secrets.token_hex(8)
    keys = load_keys(); keys[new_key] = str(interaction.guild_id); save_keys(keys)
    data = load_guild_data(interaction.guild_id); data["guild_name"] = interaction.guild.name; save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message(f"密鑰已綁定！網頁登入請輸入：`{new_key}`", ephemeral=True)

@bot.tree.command(name="set_channel", description="設定目前的頻道為通知頻道")
async def set_ch(interaction: discord.Interaction):
    data = load_guild_data(interaction.guild_id)
    data["channel_id"] = interaction.channel_id
    save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message("✅ 通知頻道設定成功！")

@bot.tree.command(name="try", description="測試通知功能是否正常")
async def try_test(interaction: discord.Interaction):
    data = load_guild_data(interaction.guild_id)
    if not data["channel_id"]:
        await interaction.response.send_message("❗ 尚未設定通知頻道，請先使用 `/set_channel`", ephemeral=True)
        return
    if not data["yt"]:
        await interaction.response.send_message("❗ 尚未新增任何 YouTube 頻道，請先去網頁端新增喔！", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    test_yt = data["yt"][0] # 拿第一個頻道來測試
    video = fetch_latest_video(test_yt['id'])
    
    if video:
        msg = translate_message(data["format"], test_yt["name"], video['link'], video['title'])
        channel = bot.get_channel(int(data["channel_id"]))
        if channel:
            await channel.send(f"🌸 **Ruixue 測試通知：**\n{msg}")
            await interaction.followup.send("💬 測試訊息已發出！快去頻道看看吧！")
        else:
            await interaction.followup.send("❌ 找不到通知頻道，可能權限不足或是頻道已被刪除。")
    else:
        await interaction.followup.send("❌ 抓取不到該頻道的最新影片資料，請稍後再試。")

# --- [Flask 網頁介面] ---
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pingall-ru | Ruixue</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #fff5f8; padding-top: 50px; font-family: 'Microsoft JhengHei', sans-serif; }
        .pink-card { border: none; border-radius: 20px; box-shadow: 0 10px 30px rgba(255,182,193,0.3); background: white; }
        .btn-pink { background: #ff85a2; color: white; border-radius: 20px; border: none; }
        .btn-pink:hover { background: #ff6b8d; color: white; }
        .preview-box { background: #fff0f3; border-radius: 15px; border: 2px dashed #ff85a2; padding: 15px; margin-bottom: 20px; }
        .video-thumb { width: 100%; border-radius: 10px; margin-top: 10px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
<div class="container">
    <div class="row justify-content-center">
        <div class="col-12 col-md-6">
            {% if not session.gid %}
            <div class="card pink-card p-4 text-center">
                <h4 style="color:#ff85a2;">🔰 管理員登入</h4>
                <form action="/login" method="post">
                    <input type="password" name="key" class="form-control mb-3 text-center rounded-pill" placeholder="請輸入密鑰" required>
                    <button type="submit" class="btn btn-pink w-100">管理伺服器</button>
                </form>
            </div>
            {% else %}
            <div class="card pink-card p-4">
                <h5 class="text-center" style="color:#ff6b8d;">🌸 {{ g_name }}</h5>
                <hr>
                {% if preview %}
                <div class="preview-box">
                    <h6 class="text-center text-muted small">✨ 最新影片預覽 ✨</h6>
                    <p class="mb-1 text-center"><strong>{{ preview.name }}</strong></p>
                    {% if preview.last_video %}
                        <p class="small text-center mb-1">{{ preview.last_video.title }}</p>
                        <img src="{{ preview.last_video.thumb }}" class="video-thumb">
                    {% else %}
                        <p class="small text-center text-danger">( 暫時抓不到影片預覽 )</p>
                    {% endif %}
                </div>
                {% endif %}
                <form action="/update_format" method="post" class="mb-4">
                    <label class="small text-muted">訊息格式 (&e, &who, &url, &str)</label>
                    <div class="input-group mt-1">
                        <input type="text" name="format" class="form-control" value="{{ current_format }}">
                        <button type="submit" class="btn btn-outline-secondary">儲存</button>
                    </div>
                </form>
                <form action="/add" method="post" class="mb-4">
                    <div class="input-group">
                        <input type="text" name="yt_id" class="form-control rounded-start-pill" placeholder="輸入 YouTube @帳號" required>
                        <button type="submit" class="btn btn-pink rounded-end-pill">新增</button>
                    </div>
                </form>
                <div class="list-group">
                    {% for yt in yt_list %}
                    <div class="list-group-item d-flex justify-content-between align-items-center border-0 shadow-sm mb-2 rounded-3">
                        <span>{{ yt.name }}</span>
                        <a href="/delete/{{ yt.id }}" class="btn btn-sm btn-danger rounded-pill">刪除</a>
                    </div>
                    {% endfor %}
                </div>
                <div class="text-center mt-3"><a href="/logout" class="text-muted small">登出系統</a></div>
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
    if not gid: return render_template_string(HTML_TEMPLATE)
    data = load_guild_data(gid)
    preview = session.pop('preview_data', None)
    return render_template_string(HTML_TEMPLATE, g_name=data['guild_name'], yt_list=data['yt'], current_format=data['format'], preview=preview)

@app.route('/login', methods=['POST'])
def login():
    key = request.form.get('key'); keys = load_keys()
    if key in keys: session['gid'] = keys[key]
    return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
def add():
    gid = session.get('gid')
    if not gid: return redirect(url_for('index'))
    info, err = verify_yt(request.form.get('yt_id'))
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

@app.route('/delete/<ytid>')
def delete(ytid):
    gid = session.get('gid')
    if gid:
        data = load_guild_data(gid)
        data['yt'] = [i for i in data['yt'] if i['id'] != ytid]; save_guild_data(gid, data)
    return redirect(url_for('index'))

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

if __name__ == "__main__":
    if not TOKEN:
        print("❌ 錯誤：找不到 DISCORD_TOKEN 環境變數！")
    else:
        Thread(target=lambda: app.run(host='0.0.0.0', port=5000)).start()
        bot.run(TOKEN)
