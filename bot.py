import os
import json
import time
import logging
import asyncio
from typing import Dict, Optional, List

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
SCHEDULE_CHANNEL_ID = os.getenv("SCHEDULE_CHANNEL_ID")  # 予定表出力先（ログ用チャンネル）

# ===== Intents =====
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # メッセージ本文が不要ならFalseでも可
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ====== 入社日程の選択肢 ======
# value は内部キー。順序は予定表の表示順にも使用
TIME_OPTIONS: List[tuple[str, str]] = [
    ("0-3時", "0-3"),
    ("3-6時", "3-6"),
    ("6-9時", "6-9"),
    ("9-12時", "9-12"),
    ("12-15時", "12-15"),
    ("15-20時", "15-20"),
    ("20-0時", "20-0"),
    ("いつでも", "anytime"),                 # ★ 追加
    ("その他（自由入力）", "other"),
]
# 予定表の縦並び順
SLOT_ORDER: List[tuple[str, str]] = [
    ("0-3時", "0-3"),
    ("3-6時", "3-6"),
    ("6-9時", "6-9"),
    ("9-12時", "9-12"),
    ("12-15時", "12-15"),
    ("15-20時", "15-20"),
    ("20-0時", "20-0"),
    ("いつでも", "anytime"),
    ("その他", "other"),
]

# ====== Sticky（最下部ボタン維持）永続 ======
STICKY_FILE = "sticky.json"          # {channel_id: message_id}
STICKY_STATE: Dict[int, int] = {}

# ====== エントリー永続（予定表生成用） ======
ENTRIES_FILE = "entries.json"
# レコード例:
# { "guild_id": 123, "channel_id": 456, "message_id": 789, "user_id": 111,
#   "name": "山田太郎", "referrer": "佐藤花子", "slot_key": "9-12",
#   "custom_time": null, "ts": 1710000000.123 }
ENTRIES: List[Dict] = []

# ====== 予定表メッセージの永続 ======
SCHEDULE_STATE_FILE = "schedule_state.json"
# { "schedule_channel_id": 123, "message_id": 456 }
SCHEDULE_STATE: Dict[str, int] = {}

# ====== 入力途中データ ======
TEMP_ENTRY: Dict[int, Dict] = {}  # user_id -> {"name":..., "referrer":..., "custom_time":...}

# ==========================
#  JSON 永続 ユーティリティ
# ==========================
def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.exception(f"Failed to load {path}: %s", e)
    return default

def _save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.exception(f"Failed to save {path}: %s", e)

def load_states():
    global STICKY_STATE, ENTRIES, SCHEDULE_STATE
    STICKY_STATE = {int(k): int(v) for k, v in _load_json(STICKY_FILE, {}).items()}
    ENTRIES = _load_json(ENTRIES_FILE, [])
    SCHEDULE_STATE = _load_json(SCHEDULE_STATE_FILE, {})

def save_sticky():
    _save_json(STICKY_FILE, {str(k): v for k, v in STICKY_STATE.items()})

def save_entries():
    _save_json(ENTRIES_FILE, ENTRIES)

def save_schedule_state():
    _save_json(SCHEDULE_STATE_FILE, SCHEDULE_STATE)

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
#  予定表（スケジュール）生成・更新
# ==========================
def _message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

def _slot_label_from_key(key: str) -> str:
    # 表示用ラベル
    for lbl, val in TIME_OPTIONS:
        if val == key:
            return "その他" if key == "other" else lbl
    # fallback
    return key

def _group_entries_by_slot() -> Dict[str, List[Dict]]:
    buckets: Dict[str, List[Dict]] = {key: [] for _, key in SLOT_ORDER}
    for e in ENTRIES:
        key = e.get("slot_key", "other")
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(e)
    # 受付順に見やすく（ts 昇順）
    for k in buckets:
        buckets[k].sort(key=lambda x: x.get("ts", 0.0))
    return buckets

def _build_schedule_embed() -> discord.Embed:
    total = len(ENTRIES)
    embed = discord.Embed(
        title=f"入社日程 予定表（合計 {total} 件）",
        description="※ ボタンからの入力内容を自動で集計しています。",
        color=discord.Color.green()
    )
    embed.set_footer(text="自動更新")

    buckets = _group_entries_by_slot()
    for label, key in SLOT_ORDER:
        items = buckets.get(key, [])
        if not items:
            value = "— なし —"
        else:
            lines = []
            for e in items:
                name = e.get("name", "不明")
                link = _message_link(e["guild_id"], e["channel_id"], e["message_id"])
                if key == "other" and e.get("custom_time"):
                    lines.append(f"- {name}（{e['custom_time']}） — [メッセージ]({link})")
                else:
                    lines.append(f"- {name} — [メッセージ]({link})")
            value = "\n".join(lines)
            # DiscordのEmbedフィールド値は最大1024文字、超える場合は末尾を省略
            if len(value) > 1024:
                value = value[:1000] + "\n…（続きあり）"
        embed.add_field(name=label, value=value, inline=False)
    return embed

async def ensure_schedule_message() -> Optional[discord.Message]:
    """予定表メッセージ（1つ）を SCHEDULE_CHANNEL_ID に確保し、Message を返す。"""
    if not SCHEDULE_CHANNEL_ID:
        log.info("SCHEDULE_CHANNEL_ID 未設定のため、予定表は出力しません。")
        return None

    channel = bot.get_channel(int(SCHEDULE_CHANNEL_ID))
    if not isinstance(channel, discord.TextChannel):
        log.warning("SCHEDULE_CHANNEL_ID がテキストチャンネルではありません。")
        return None

    msg_id = SCHEDULE_STATE.get("message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            return msg
        except discord.NotFound:
            pass
        except Exception as e:
            log.exception("fetch schedule message failed: %s", e)

    # なければ新規作成
    try:
        embed = _build_schedule_embed()
        msg = await channel.send(embed=embed)
        SCHEDULE_STATE["schedule_channel_id"] = channel.id
        SCHEDULE_STATE["message_id"] = msg.id
        save_schedule_state()
        return msg
    except Exception as e:
        log.exception("create schedule message failed: %s", e)
        return None

async def update_schedule_panel():
    """予定表メッセージを現在の ENTRIES で上書き更新。"""
    msg = await ensure_schedule_message()
    if not msg:
        return
    try:
        embed = _build_schedule_embed()
        await msg.edit(embed=embed)
    except Exception as e:
        log.exception("update schedule message failed: %s", e)

def add_entry_record(guild_id: int, channel_id: int, message_id: int,
                     user_id: int, name: str, referrer: str,
                     slot_key: str, custom_time: Optional[str]):
    ENTRIES.append({
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "user_id": int(user_id),
        "name": name,
        "referrer": referrer,
        "slot_key": slot_key,
        "custom_time": custom_time,
        "ts": time.time(),
    })
    save_entries()

# ==========================
#  Sticky（最下部ボタン維持）
# ==========================
STICKY_TEXT = "やあ、よく来たね。入社日程について話そう"
_channel_locks: Dict[int, asyncio.Lock] = {}

def _channel_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]

async def post_sticky_message(channel: discord.TextChannel) -> Optional[int]:
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
    except Exception:
        pass

async def ensure_sticky_bottom(channel: discord.TextChannel):
    lock = _channel_lock(channel.id)
    async with lock:
        last_msg: Optional[discord.Message] = None
        async for m in channel.history(limit=1):
            last_msg = m
            break

        current_sticky_id = STICKY_STATE.get(channel.id)
        if last_msg and current_sticky_id and last_msg.id == current_sticky_id:
            return

        if current_sticky_id:
            await delete_message_if_exists(channel, current_sticky_id)

        new_id = await post_sticky_message(channel)
        if new_id:
            STICKY_STATE[channel.id] = new_id
            save_sticky()

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
    # 送信者（クリックでプロフィールへ）
    embed.set_author(
        name=f"{user.display_name}（ID: {user.id}）",
        icon_url=user.display_avatar.url,
        url=f"https://discord.com/users/{user.id}"
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    # 入力内容
    embed.add_field(name="お名前", value=data["name"], inline=False)
    embed.add_field(name="入社日程", value=schedule_text, inline=False)
    embed.add_field(name="紹介者", value=data["referrer"], inline=False)
    embed.add_field(name="Discord ID", value=str(user.id), inline=False)
    embed.add_field(
        name="プロフィール",
        value=f"[プロフィールを開く](https://discord.com/users/{user.id})\n{user.mention}",
        inline=False
    )

    # 同じチャンネルへ投稿（なければDM）
    target_channel = interaction.channel or (await user.create_dm())
    sent_msg = await target_channel.send(embed=embed)

    # 永続にレコード追加（予定表用）
    try:
        guild_id = interaction.guild.id if interaction.guild else 0
        add_entry_record(
            guild_id=guild_id,
            channel_id=sent_msg.channel.id,
            message_id=sent_msg.id,
            user_id=user.id,
            name=data["name"],
            referrer=data["referrer"],
            slot_key=chosen_value,                 # "0-3" 等 / "anytime" / "other"
            custom_time=data.get("custom_time"),
        )
    except Exception as e:
        log.exception("add_entry_record failed: %s", e)

    # 一時データ破棄
    TEMP_ENTRY.pop(user.id, None)

    # エフェメラル通知（未応答/応答済みで分岐）
    if not interaction.response.is_done():
        await interaction.response.send_message("送信しました。ありがとうございます！", ephemeral=True)
    else:
        await interaction.followup.send("送信しました。ありがとうございます！", ephemeral=True)

    # “最下部ボタン”維持
    if isinstance(target_channel, discord.TextChannel):
        await ensure_sticky_bottom(target_channel)

    # 予定表を更新
    await update_schedule_panel()

# ==========================
#  Slash コマンド
# ==========================
@tree.command(description="入社日程案内のボタンを“最下部に常時表示”として設置します")
async def entry_panel(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("このコマンドはテキストチャンネルで使ってください。", ephemeral=True)
        return
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
        await delete_message_if_exists(interaction.channel, msg_id)
        await interaction.response.send_message("このチャンネルの最下部ボタンを解除しました。", ephemeral=True)
    else:
        await interaction.response.send_message("このチャンネルには有効な最下部ボタンがありません。", ephemeral=True)

@tree.command(description="予定表を手動で再生成・更新します（ログチャンネル）")
async def schedule_refresh(interaction: discord.Interaction):
    await update_schedule_panel()
    await interaction.response.send_message("予定表を更新しました。", ephemeral=True)

@tree.command(description="疎通確認（/ping）")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")

# ==========================
#  メッセージイベント：だれかが発言したら “最下部ボタン” を底に再配置
# ==========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if isinstance(message.channel, discord.TextChannel) and message.channel.id in STICKY_STATE:
        await ensure_sticky_bottom(message.channel)

# ==========================
#  起動時処理
# ==========================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_states()

    # 再起動後もボタンUIを有効化
    bot.add_view(EntryButtonView())

    # Sticky 整合性の調整
    for ch_id in list(STICKY_STATE.keys()):
        channel = bot.get_channel(ch_id)
        if isinstance(channel, discord.TextChannel):
            asyncio.create_task(ensure_sticky_bottom(channel))

    # 予定表メッセージの確保＆初期更新
    asyncio.create_task(update_schedule_panel())

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
    try:
        from keep_alive import run_server
        asyncio.get_event_loop().create_task(run_server())
    except Exception:
        log.warning("keep_alive サーバーは起動しませんでした（ローカルなど）")
    main()
