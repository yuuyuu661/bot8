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
GUILD_ID = os.getenv("GUILD_ID")  # 即時同期（任意。数値文字列）
SYNC_ON_START = os.getenv("SYNC_ON_START", "1") == "1"
SCHEDULE_CHANNEL_ID = os.getenv("SCHEDULE_CHANNEL_ID")  # 予定表出力先チャンネルID（数値文字列）
ENTRY_MANAGER_ROLE_ID = int(os.getenv("ENTRY_MANAGER_ROLE_ID", "1398724601256874014"))

# ===== 保存ディレクトリ =====
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

STICKY_FILE = os.path.join(DATA_DIR, "sticky.json")
ENTRIES_FILE = os.path.join(DATA_DIR, "entries.json")
SCHEDULE_STATE_FILE = os.path.join(DATA_DIR, "schedule_state.json")

# ===== Intents / Bot =====
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ====== 入社日程の選択肢 ======
TIME_OPTIONS: List[tuple[str, str]] = [
    ("0-3時", "0-3"),
    ("3-6時", "3-6"),
    ("6-9時", "6-9"),
    ("9-12時", "9-12"),
    ("12-15時", "12-15"),
    ("15-20時", "15-20"),
    ("20-0時", "20-0"),
    ("いつでも", "anytime"),
    ("その他（自由入力）", "other"),
]
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

# ====== 状態保持 ======
STICKY_STATE: Dict[int, int] = {}       # {channel_id: message_id}
ENTRIES: List[Dict] = []                # 例：{guild_id, channel_id, message_id, user_id, name, referrer, slot_key, custom_time, status, ts}
SCHEDULE_STATE: Dict[str, int] = {}     # {"schedule_channel_id": ..., "message_id": ...}
TEMP_ENTRY: Dict[int, Dict] = {}        # user_id -> {"name":..., "referrer":..., "custom_time":...}

# ====== JSONユーティリティ ======
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
    ENTRIES[:] = _load_json(ENTRIES_FILE, [])
    SCHEDULE_STATE.update(_load_json(SCHEDULE_STATE_FILE, {}))

def save_sticky():
    _save_json(STICKY_FILE, {str(k): v for k, v in STICKY_STATE.items()})

def save_entries():
    _save_json(ENTRIES_FILE, ENTRIES)

def save_schedule_state():
    _save_json(SCHEDULE_STATE_FILE, SCHEDULE_STATE)

# ====== 重複登録防止 ======
# active または interviewed を持っているユーザーは再エントリー不可
BLOCK_STATUSES = {"active", "interviewed"}

def user_has_blocking_entries(user_id: int) -> bool:
    uid = int(user_id)
    return any(e.get("user_id") == uid and e.get("status", "active") in BLOCK_STATUSES for e in ENTRIES)

# ==========================
#  モーダル
# ==========================
class BasicInfoModal(discord.ui.Modal, title="入社日程：基本情報"):
    name = discord.ui.TextInput(label="お名前", placeholder="例）山田 太郎", required=True, max_length=50)
    referrer = discord.ui.TextInput(label="紹介者", placeholder="例）佐藤 花子（いなければ『なし』）", required=True, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        if user_has_blocking_entries(interaction.user.id):
            return await interaction.response.send_message("すでに登録済み、または面接済みのため再エントリーはできません。", ephemeral=True)
        TEMP_ENTRY[interaction.user.id] = {
            "name": str(self.name),
            "referrer": str(self.referrer),
            "custom_time": None,
        }
        view = TimeSelectView()
        await interaction.response.send_message("入社日程を選択してください。", view=view, ephemeral=True)

class CustomTimeModal(discord.ui.Modal, title="入社日程：自由入力（その他）"):
    custom_time = discord.ui.TextInput(
        label="入社日程（自由入力）",
        placeholder="例）来週水曜の午後／○月○日 10時〜 など",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        if user_has_blocking_entries(interaction.user.id):
            return await interaction.response.send_message("すでに登録済み、または面接済みのため再エントリーはできません。", ephemeral=True)
        data = TEMP_ENTRY.get(interaction.user.id)
        if not data:
            return await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
        data["custom_time"] = str(self.custom_time)
        await post_panel_and_confirm(interaction, chosen_labels=["その他"], chosen_values=["other"])

# ==========================
#  セレクト・ビュー
# ==========================
class TimeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=value) for (label, value) in TIME_OPTIONS]
        super().__init__(
            placeholder="入社日程を選んでください（最大2つ / その他は単独選択）",
            min_values=1, max_values=2, options=options, custom_id="select_join_time"
        )

    async def callback(self, interaction: discord.Interaction):
        if user_has_blocking_entries(interaction.user.id):
            return await interaction.response.send_message("すでに登録済み、または面接済みのため再エントリーはできません。", ephemeral=True)
        values = list(self.values)
        # 「その他」は単独選択のみ
        if "other" in values and len(values) > 1:
            return await interaction.response.send_message("「その他」は単独で選択してください。", ephemeral=True)
        if "other" in values:
            return await interaction.response.send_modal(CustomTimeModal())
        # ラベル解決
        pairs = [(lbl, val) for (lbl, val) in TIME_OPTIONS if val in values]
        chosen_labels = [lbl for (lbl, _) in pairs]
        chosen_values = [val for (_, val) in pairs]
        await post_panel_and_confirm(interaction, chosen_labels, chosen_values)

class TimeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(TimeSelect())

class EntryButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="入社日程記入", style=discord.ButtonStyle.primary, custom_id="entry_button_open_modal")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BasicInfoModal())

# ==========================
#  スケジュールEmbed関連
# ==========================
def _message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

def _group_entries_by_slot() -> Dict[str, List[Dict]]:
    buckets: Dict[str, List[Dict]] = {key: [] for _, key in SLOT_ORDER}
    for e in ENTRIES:
        if e.get("status", "active") != "active":
            continue
        buckets.setdefault(e.get("slot_key", "other"), []).append(e)
    for k in buckets:
        buckets[k].sort(key=lambda x: x.get("ts", 0.0))
    return buckets

def _build_schedule_embed() -> discord.Embed:
    total_active = sum(1 for e in ENTRIES if e.get("status", "active") == "active")
    embed = discord.Embed(
        title=f"入社日程 予定表（現在 {total_active} 件）",
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
                uid = e.get("user_id")
                mention = f"<@{uid}>" if uid else ""
                link = _message_link(e["guild_id"], e["channel_id"], e["message_id"])
                if key == "other" and e.get("custom_time"):
                    lines.append(f"- {name} {mention}（{e['custom_time']}） — [メッセージ]({link})")
                else:
                    lines.append(f"- {name} {mention} — [メッセージ]({link})")
            value = "\n".join(lines)
            if len(value) > 1024:
                value = value[:1000] + "\n…（続きあり）"
        embed.add_field(name=label, value=value, inline=False)
    return embed

async def ensure_schedule_message() -> Optional[discord.Message]:
    if not SCHEDULE_CHANNEL_ID:
        return None
    channel = bot.get_channel(int(SCHEDULE_CHANNEL_ID))
    if not isinstance(channel, discord.TextChannel):
        return None
    msg_id = SCHEDULE_STATE.get("message_id")
    if msg_id:
        try:
            return await channel.fetch_message(int(msg_id))
        except discord.NotFound:
            pass
        except Exception as e:
            log.exception("fetch schedule message failed: %s", e)
    try:
        msg = await channel.send(
            embed=_build_schedule_embed(),
            allowed_mentions=discord.AllowedMentions.none()
        )
        SCHEDULE_STATE["schedule_channel_id"] = channel.id
        SCHEDULE_STATE["message_id"] = msg.id
        save_schedule_state()
        return msg
    except Exception as e:
        log.exception("create schedule message failed: %s", e)
        return None

async def update_schedule_panel():
    msg = await ensure_schedule_message()
    if msg:
        try:
            await msg.edit(
                embed=_build_schedule_embed(),
                allowed_mentions=discord.AllowedMentions.none()
            )
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
        "status": "active",
        "ts": time.time(),
    })
    save_entries()

# ==========================
#  “最下部ボタン”維持（スティッキー）
# ==========================
STICKY_TEXT = "やあ、よく来たね。入社日程について話そう"
_channel_locks: Dict[int, asyncio.Lock] = {}
_sticky_cooldown: Dict[int, float] = {}
STICKY_COOLDOWN_SEC = 3.0

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
    now = time.time()
    last = _sticky_cooldown.get(channel.id, 0.0)
    if now - last < STICKY_COOLDOWN_SEC:
        return
    _sticky_cooldown[channel.id] = now

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
#  パネル送信（複数枠対応）＆ステータス操作
# ==========================
class EntryStatusControlView(discord.ui.View):
    """エントリーパネル下の操作ボタン（面接済み / 応答無し）。永続ビュー。"""
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        if disabled:
            for c in self.children:
                if isinstance(c, discord.ui.Button):
                    c.disabled = True

    def _has_perm(self, member: Optional[discord.Member]) -> bool:
        return isinstance(member, discord.Member) and any(r.id == ENTRY_MANAGER_ROLE_ID for r in member.roles)

    async def _handle_status_change(self, interaction: discord.Interaction, status_key: str, status_label: str):
        if not self._has_perm(interaction.user):
            return await interaction.response.send_message("権限がありません。", ephemeral=True)

        msg = interaction.message
        entry = next((e for e in ENTRIES if e.get("message_id") == msg.id), None)
        if not entry or entry.get("status", "active") != "active":
            return await interaction.response.send_message("対象エントリーが見つかりません。", ephemeral=True)

        if status_key == "interviewed":
            # 同一ユーザーの active をすべて面接済みに
            uid = entry.get("user_id")
            for e in ENTRIES:
                if e.get("user_id") == uid and e.get("status", "active") == "active":
                    e["status"] = "interviewed"
            save_entries()
        elif status_key == "no_response":
            # 同一メッセージIDに紐づく active 枠をすべて応答無しに
            mid = entry.get("message_id")
            changed = 0
            for e in ENTRIES:
                if e.get("message_id") == mid and e.get("status", "active") == "active":
                    e["status"] = "no_response"
                    changed += 1
            if changed:
                save_entries()
        else:
            entry["status"] = status_key
            save_entries()

        try:
            await msg.edit(view=EntryStatusControlView(disabled=True))
        except Exception:
            pass

        await update_schedule_panel()
        await interaction.response.send_message(f"「{status_label}」に更新しました。", ephemeral=True)

    @discord.ui.button(label="面接済み", style=discord.ButtonStyle.success, custom_id="entry_mark_interviewed")
    async def interviewed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_status_change(interaction, "interviewed", "面接済み")

    @discord.ui.button(label="応答無し", style=discord.ButtonStyle.secondary, custom_id="entry_mark_no_response")
    async def no_response(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_status_change(interaction, "no_response", "応答無し")

async def post_panel_and_confirm(interaction: discord.Interaction, chosen_labels: List[str], chosen_values: List[str]):
    user = interaction.user
    data = TEMP_ENTRY.pop(user.id, None)
    if not data:
        return await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)

    # 表示用（複数は " / " で連結。その他は手入力）
    display = " / ".join(data["custom_time"] if v == "other" else l for l, v in zip(chosen_labels, chosen_values))

    # エントリーパネル送信
    embed = discord.Embed(title="入社エントリー", description="以下の内容で受付しました。", color=discord.Color.blue())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="お名前", value=data["name"], inline=False)
    embed.add_field(name="入社日程", value=display, inline=False)
    embed.add_field(name="紹介者", value=data["referrer"], inline=False)

    target_channel = interaction.channel or (await user.create_dm())
    sent_msg = await target_channel.send(embed=embed, view=EntryStatusControlView())

    # レコード保存（選択枠の数だけ登録）
    guild_id = interaction.guild.id if interaction.guild else 0
    for val in chosen_values:
        add_entry_record(
            guild_id=guild_id,
            channel_id=sent_msg.channel.id,
            message_id=sent_msg.id,
            user_id=user.id,
            name=data["name"],
            referrer=data["referrer"],
            slot_key=val,
            custom_time=data.get("custom_time") if val == "other" else None,
        )

    await interaction.response.send_message("送信しました。ありがとうございます！", ephemeral=True)

    if isinstance(target_channel, discord.TextChannel):
        await ensure_sticky_bottom(target_channel)
    await update_schedule_panel()

# ==========================
#  コマンド
# ==========================
@tree.command(description="入社日程案内のボタンを“最下部に常時表示”として設置します")
async def entry_panel(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("このコマンドはテキストチャンネルで使ってください。", ephemeral=True)
    await interaction.response.send_message("最下部にボタンを設置します。", ephemeral=True)
    await ensure_sticky_bottom(interaction.channel)

@tree.command(description="このチャンネルの“最下部ボタン”を解除します")
async def entry_panel_off(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("このコマンドはテキストチャンネルで使ってください。", ephemeral=True)
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

@tree.command(description="エントリー削除（管理者用）")
@app_commands.describe(
    message_id="エントリーパネルのメッセージID（リンク末尾の数字）",
    slot="時間帯を指定するとその枠のみ削除。未指定で全枠削除"
)
@app_commands.choices(slot=[app_commands.Choice(name=label, value=value) for (label, value) in SLOT_ORDER])
async def entry_delete(
    interaction: discord.Interaction,
    message_id: str,
    slot: Optional[app_commands.Choice[str]] = None
):
    # 権限チェック
    if not isinstance(interaction.user, discord.Member) or not any(r.id == ENTRY_MANAGER_ROLE_ID for r in interaction.user.roles):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_idは数値で指定してください。", ephemeral=True)

    slot_key = slot.value if slot else None
    count = 0
    for e in ENTRIES:
        if e.get("message_id") == mid and e.get("status", "active") == "active":
            if slot_key is None or e.get("slot_key") == slot_key:
                e["status"] = "deleted"
                count += 1
    if count:
        save_entries()
        await update_schedule_panel()
    await interaction.response.send_message(f"{count}件削除しました。", ephemeral=True)

@tree.command(description="Botにメッセージを送らせます（管理者用）")
@app_commands.describe(
    content="送信する本文（改行可）",
    channel="送信先（未指定なら今のチャンネル）",
    allow_mentions="@everyone/@here/ロール/ユーザーのメンションを有効化（既定:無効）"
)
async def say(
    interaction: discord.Interaction,
    content: str,
    channel: Optional[discord.TextChannel] = None,
    allow_mentions: Optional[bool] = False
):
    # 権限チェック
    if not isinstance(interaction.user, discord.Member) or not any(r.id == ENTRY_MANAGER_ROLE_ID for r in interaction.user.roles):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行するか、送信先チャンネルを指定してください。", ephemeral=True)

    mentions = (discord.AllowedMentions.all() if allow_mentions else discord.AllowedMentions.none())
    try:
        await target.send(content, allowed_mentions=mentions)
        await interaction.response.send_message(f"送信しました → {target.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("送信先で権限不足です（SEND_MESSAGES）。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"送信に失敗しました：{e}", ephemeral=True)

@tree.command(description="疎通確認（/ping）")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")

# ==========================
#  イベント
# ==========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if isinstance(message.channel, discord.TextChannel) and message.channel.id in STICKY_STATE:
        await ensure_sticky_bottom(message.channel)

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_states()

    # 永続ビュー再登録
    bot.add_view(EntryButtonView())
    bot.add_view(EntryStatusControlView())

    # 既存スティッキー整合
    for ch_id in list(STICKY_STATE.keys()):
        channel = bot.get_channel(ch_id)
        if isinstance(channel, discord.TextChannel):
            asyncio.create_task(ensure_sticky_bottom(channel))

    # 予定表 初期更新
    asyncio.create_task(update_schedule_panel())

    # スラッシュ同期
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
        # keep_alive 側はスレッド等でブロックしない実装を想定（無ければ例外→スキップ）
        from keep_alive import run_server
        run_server()
    except Exception:
        log.warning("keep_alive サーバーは起動しませんでした（ローカルなど）")
    main()
