import os
import json
import logging
import asyncio
from typing import Dict, Optional

import discord
from discord.ext import commands
from discord import app_commands

# ===== ロギング =====
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s"
)
log = logging.getLogger("bot")

# ===== 環境変数 =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 即時反映したいギルド（任意）
SYNC_ON_START = os.getenv("SYNC_ON_START", "1") == "1"

# ===== Intents =====
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # テキスト内容が不要ならFalseでも可
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ====== 入社日程の選択肢 ======
TIME_OPTIONS = [
    ("0-3時", "0-3"),
    ("3-6時", "3-6"),
    ("6-9時", "6-9"),
    ("9-12時", "9-12"),
    ("12-15時", "12-15"),
    ("15-20時", "15-20"),
    ("20-0時", "20-0"),
    ("その他（自由入力）", "other"),
]

# ====== “最下部に常時ボタン” のための永続（簡易JSON） ======
STICKY_FILE = "sticky.json"
# 形式: {"<channel_id>": "<message_id>"}
STICKY_STATE: Dict[int, int] = {}  # チャンネルID -> 直近のボタンメッセージID

def load_sticky():
    global STICKY_STATE
    if os.path.exists(STICKY_FILE):
        try:
            with open(STICKY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            STICKY_STATE = {int(k): int(v) for k, v in data.items()}
            log.info(f"Loaded sticky state: {STICKY_STATE}")
        except Exception as e:
            log.exception("Failed to load sticky.json: %s", e)

def save_sticky():
    try:
        with open(STICKY_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in STICKY_STATE.items()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.exception("Failed to save sticky.json: %s", e)

# ====== 入力途中データ ======
TEMP_ENTRY: Dict[int, Dict] = {}  # user_id -> {"name":..., "referrer":..., "custom_time":...}

# ==========================
#  モーダル
# ==========================
class BasicInfoModal(discord.ui.Modal, title="入社日程：基本情報"):
    name = discord.ui.TextInput(
        label="お名前",
        placeholder="例）山田 太郎",
        required=True,
        max_length=50
    )
    referrer = discord.ui.TextInput(
        label="紹介者",
        placeholder="例）佐藤 花子（いなければ『なし』）",
        required=True,
        max_length=50
    )

    def __init__(self):
        super().__init__(timeout=None)

    async def on_submit(self, interaction: discord.Interaction):
        TEMP_ENTRY[interaction.user.id] = {
            "name": str(self.name),
            "referrer": str(self.referrer),
            "custom_time": None,
        }
        view = TimeSelectView()
        await interaction.response.send_message(
            "入社日程を選択してください。",
            view=view,
            ephemeral=True
        )

class CustomTimeModal(discord.ui.Modal, title="入社日程：自由入力（その他）"):
    custom_time = discord.ui.TextInput(
        label="入社日程（自由入力）",
        placeholder="例）来週水曜の午後／○月○日 10時〜 など",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        data = TEMP_ENTRY.get(interaction.user.id)
        if not data:
            if not interaction.response.is_done():
                await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
            else:
                await interaction.followup.send("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
            return

        data["custom_time"] = str(self.custom_time)
        await post_panel_and_confirm(interaction, chosen_label="その他", chosen_value="other")

# ==========================
#  セレクト・ビュー
# ==========================
class TimeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=value) for (label, value) in TIME_OPTIONS]
        super().__init__(
            placeholder="入社日程を選んでください",
            min_values=1, max_values=1,
            options=options,
            custom_id="select_join_time"
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        label = next((lbl for lbl, val in TIME_OPTIONS if val == value), value)

        if value == "other":
            await interaction.response.send_modal(CustomTimeModal())
        else:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await post_panel_and_confirm(interaction, chosen_label=label, chosen_value=value)

class TimeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(TimeSelect())

class EntryButtonView(discord.ui.View):
    """永続ビュー：再起動後もボタンは有効"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="入社日程記入",
        style=discord.ButtonStyle.primary,
        custom_id="entry_button_open_modal"
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BasicInfoModal())

# ==========================
#  パネル投稿（Embed）共通処理
# ==========================
async def post_panel_and_confirm(interaction: discord.Interaction, chosen_label: str, chosen_value: str):
    user = interaction.user
    data = TEMP_ENTRY.get(user.id)
    if not data:
        if not interaction.response.is_done():
            await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
        else:
            await interaction.followup.send("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
        return

    # 入社日程の表示文
    schedule_text = data.get("custom_time") if chosen_value == "other" else chosen_label
    if not schedule_text:
        schedule_text = "（自由入力なし）"

    # ===== Embed（パネル）=====
    embed = discord.Embed(
        title="入社エントリー",
        description="以下の内容で受付しました。",
        color=discord.Color.blue()
    )

    # ✅ 送信者の横にユーザーIDを表示し、クリックでプロフィールへ飛べるように
    #   - author行はクリック可能（URLに https://discord.com/users/<ID> を指定）
    #   - 送信者のアイコンも表示されます
    embed.set_author(
        name=f"{user.display_name}（ID: {user.id}）",
        icon_url=user.display_avatar.url,
        url=f"https://discord.com/users/{user.id}"
    )

    # プロフィール画像（サムネイル）も従来通り表示
    embed.set_thumbnail(url=user.display_avatar.url)

    # 入力内容
    embed.add_field(name="お名前", value=data["name"], inline=False)
    embed.add_field(name="入社日程", value=schedule_text, inline=False)
    embed.add_field(name="紹介者", value=data["referrer"], inline=False)

    # 元仕様どおり Discord ID も項目として掲載（author横にもあるが要件維持のため）
    embed.add_field(name="Discord ID", value=str(user.id), inline=False)

    # おまけ：プロフィール直リンク/メンションも置いておく（クリック手段をもう1つ用意）
    embed.add_field(
        name="プロフィール",
        value=f"[プロフィールを開く](https://discord.com/users/{user.id})\n{user.mention}",
        inline=False
    )

    # 同じチャンネルへ投稿（なければDM）
    target_channel = interaction.channel or (await user.create_dm())
    await target_channel.send(embed=embed)

    # 一時データ破棄
    TEMP_ENTRY.pop(user.id, None)

    # エフェメラル通知（未応答/応答済みで分岐）
    if not interaction.response.is_done():
        await interaction.response.send_message("送信しました。ありがとうございます！", ephemeral=True)
    else:
        await interaction.followup.send("送信しました。ありがとうございます！", ephemeral=True)

    # “最下部ボタン”維持（あなたの実装に合わせて）
    if isinstance(target_channel, discord.TextChannel):
        await ensure_sticky_bottom(target_channel)

# ==========================
#  “最下部ボタン”ユーティリティ
# ==========================
STICKY_TEXT = "やあ、よく来たね。入社日程について話そう"
_channel_locks: Dict[int, asyncio.Lock] = {}

def _channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]

async def post_sticky_message(channel: discord.TextChannel) -> Optional[int]:
    """ボタン付きメッセージを投稿し、そのメッセージIDを返す。"""
    try:
        view = EntryButtonView()
        msg = await channel.send(STICKY_TEXT, view=view)
        return msg.id
    except discord.Forbidden:
        log.warning(f"Missing permissions to send sticky in #{channel.id}")
    except Exception as e:
        log.exception("Failed to send sticky message: %s", e)
    return None

async def delete_message_if_exists(channel: discord.TextChannel, message_id: int):
    """指定メッセージを削除（なければ無視）。"""
    try:
        msg = await channel.fetch_message(message_id)
    except discord.NotFound:
        return
    except discord.Forbidden:
        log.warning(f"Missing permissions to delete sticky in #{channel.id}")
        return
    except Exception:
        return

    try:
        await msg.delete()
    except discord.Forbidden:
        log.warning(f"Missing permissions to delete sticky in #{channel.id}")
    except discord.NotFound:
        pass
    except Exception as e:
        log.exception("Failed to delete sticky message: %s", e)

async def ensure_sticky_bottom(channel: discord.TextChannel):
    """チャンネルの一番下にボタン付きメッセージを維持する。"""
    lock = _channel_lock(channel.id)
    async with lock:
        # 直近の投稿を取得
        last_msg: Optional[discord.Message] = None
        async for m in channel.history(limit=1):
            last_msg = m
            break

        current_sticky_id = STICKY_STATE.get(channel.id)

        # すでに最後のメッセージが“現行のSticky”なら何もしない
        if last_msg and current_sticky_id and last_msg.id == current_sticky_id:
            return

        # 既存のStickyを削除（権限なければスキップ）
        if current_sticky_id:
            await delete_message_if_exists(channel, current_sticky_id)

        # 新しいStickyを投稿
        new_id = await post_sticky_message(channel)
        if new_id:
            STICKY_STATE[channel.id] = new_id
            save_sticky()

# ==========================
#  Slash コマンド
# ==========================
@tree.command(description="入社日程案内のボタンを“最下部に常時表示”として設置します")
async def entry_panel(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("このコマンドはテキストチャンネルで使ってください。", ephemeral=True)
        return

    # その場でStickyを作成＆保存
    await interaction.response.send_message("最下部にボタンを設置します。", ephemeral=True)
    await ensure_sticky_bottom(interaction.channel)

@tree.command(description="このチャンネルの“最下部ボタン”を解除します")
async def entry_panel_off(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("このコマンドはテキストチャンネルで使ってください。", ephemeral=True)
        return

    ch_id = interaction.channel.id
    msg_id = STICKY_STATE.pop(ch_id, None)
    save_sticky()

    if msg_id:
        # 既存のStickyを削除
        await delete_message_if_exists(interaction.channel, msg_id)
        await interaction.response.send_message("このチャンネルの最下部ボタンを解除しました。", ephemeral=True)
    else:
        await interaction.response.send_message("このチャンネルには有効な最下部ボタンがありません。", ephemeral=True)

@tree.command(description="疎通確認（/ping）")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")

# ==========================
#  メッセージイベント：だれかが発言したら “最下部ボタン” を底に再配置
# ==========================
@bot.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージは無視
    if message.author.bot:
        return

    # コマンドも処理する
    await bot.process_commands(message)

    # “最下部ボタン”対象チャンネルなら底に維持
    if isinstance(message.channel, discord.TextChannel) and message.channel.id in STICKY_STATE:
        await ensure_sticky_bottom(message.channel)

# ==========================
#  起動時処理
# ==========================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_sticky()
    # 再起動後もボタンUIを有効化
    bot.add_view(EntryButtonView())

    # 再接続時、既存Stickyの整合がズレていることがあるので軽く修正
    for ch_id in list(STICKY_STATE.keys()):
        channel = bot.get_channel(ch_id)
        if isinstance(channel, discord.TextChannel):
            # 背景で整える（大量チャンネルでも順次処理）
            asyncio.create_task(ensure_sticky_bottom(channel))

    if SYNC_ON_START:
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                synced = await tree.sync(guild=guild)
                log.info(f"Synced {len(synced)} commands to guild {GUILD_ID}")
            else:
                synced = await tree.sync()
                log.info(f"Synced {len(synced)} global commands")
        except Exception as e:
            log.exception("Failed to sync commands: %s", e)

# ==========================
#  エントリーポイント
# ==========================
def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が未設定です。Railway Variables で設定してください。")
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    # Railway のヘルスチェック用サーバー（任意）
    try:
        from keep_alive import run_server
        asyncio.get_event_loop().create_task(run_server())
    except Exception:
        log.warning("keep_alive サーバーは起動しませんでした（ローカルなど）")
    main()

