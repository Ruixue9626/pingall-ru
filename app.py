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

# 讀取環境變數 (建議使用 .env 檔案)
load_dotenv()

# --- [設定與資料處理] ---
# 🌸 建議在大主人的資料夾建立 .env 檔案，內容：DISCORD_TOKEN=你的TOKEN
TOKEN = os.getenv('DISCORD_TOKEN') or '你的TOKEN放這裡'
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

# --- [YouTube 抓取引擎：Shorts 強化版] ---
def fetch_latest_video(channel_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-TW,zh;q=0.9'
    }
    
    candidates = []

    # 1. RSS Feed (抓一般影片)
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
                    "published": time.mktime(e.published_parsed) if 'published_parsed' in e else 0
                })
    except: pass

    # 2. 爬取 /shorts 頁面 (抓短影音)
    try:
        r_shorts = requests.get(f"https://www.youtube.com/channel/{channel_id}/shorts", headers=headers, timeout=10)
        s_match = re.search(r'"videoId":"([^"]+)"', r_shorts.text)
        t_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"', r_shorts.text)
        if s_match:
            vid = s_match.group(1)
            candidates.append({
                "title": html.unescape(t_match.group(1)) if t_match else "最新 Shorts",
                "link": f"https://www.youtube.com/shorts/{vid}",
                "published": time.time() # 當作最新
            })
    except: pass

    if candidates:
        # 選出看起來最新的一個
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

        if not channel_id: return None, "找不到頻道 ID"
        return {"id": channel_id, "name": name}, None
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
                await asyncio.sleep(2)

bot = RuixueBot()

@bot.tree.command(name="set_channel", description="設定目前的頻道為通知頻道")
async def set_ch(interaction: discord.Interaction):
    data = load_guild_data(interaction.guild_id)
    data["channel_id"] = str(interaction.channel_id)
    save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message("✅ 通知頻道設定成功！")

@bot.tree.command(name="git", description="申請管理密鑰")
async def git_key(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("只有管理員可以申請喔", ephemeral=True)
        return
    new_key = secrets.token_hex(8)
    keys = load_keys(); keys[new_key] = str(interaction.guild_id); save_keys(keys)
    data = load_guild_data(interaction.guild_id); data["guild_name"] = interaction.guild.name; save_guild_data(interaction.guild_id, data)
    await interaction.response.send_message(f"密鑰已綁定！網頁登入請輸入：`{new_key}`", ephemeral=True)

@bot.tree.command(name="try", description="測試通知是否正常")
async def try_test(interaction: discord.Interaction):
    data = load_guild_data(interaction.guild_id)
    
    # 🌸 偵錯邏輯修正
    if not data.get("channel_id"):
        await interaction.response.send_message("❗ 尚未設定通知頻道，請先使用 `/set_channel` 喔！", ephemeral=True)
        return
    if not data.get("yt"):
        await interaction.response.send_message("❗ 追隨清單是空的，請先去網頁端新增 YouTube 頻道喔！", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    test_yt = data["yt"][0]
    video = fetch_latest_video(test_yt['id'])
    
    if video:
        msg = translate_message(data["format"], test_yt["name"], video['link'], video['title'])
        channel = bot.get_channel(int(data["channel_id"]))
        if channel:
            await channel.send(f"✅ **Pingall-ru 測試成功：**\n{msg}")
            await interaction.followup.send("💬 測試訊息發出去了！快去頻道看看～")
        else:
            await interaction.followup.send("❌ 找不到頻道，請確認機器人是否有權限看該頻道。")
    else:
        await interaction.followup.send("❌ 抓不到頻道資料 (Shorts 或 影片)，請稍後再試。")

# --- [Flask 網頁介面維持原樣] ---
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# (HTML 模板部分省略，維持與原本相同...)
# [省略部分以節省篇幅，內容包含管理員登入與格式設定]

@app.route('/')
def index():
    gid = session.get('gid')
    if not gid: return "請先在 Discord 使用 /git 登入"
    data = load_guild_data(gid)
    return f"伺服器：{data['guild_name']}，目前清單有 {len(data['yt'])} 個頻道。"

# (其他 Flask 路由：login, add, delete... 維持原樣)

if __name__ == "__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=5000)).start()
    bot.run(TOKEN)
