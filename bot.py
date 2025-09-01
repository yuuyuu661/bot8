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
GUILD_ID = os.getenv("GUILD_ID")  # 即時同期（任意）
SYNC_ON_START = os.getenv("SYNC_ON_START", "1") == "1"
SCHEDULE_CHANNEL_ID = os.getenv("SCHEDULE_CHANNEL_ID")  # 予定表出力先
ENTRY_MANAGER_ROLE_ID = int(os.getenv("ENTRY_MANAGER_ROLE_ID", "1398724601256874014"))

# ===== 保存ディレクトリ =====
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

STICKY_FILE = os.path.join(DATA_DIR, "sticky.json")
ENTRIES_FILE = os.path.join(DATA_DIR, "entries.json")
SCHEDULE_STATE_FILE = os.path.join(DATA_DIR, "schedule_state.json")

# ===== Intents =====
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
STICKY_STATE: Dict[int, int] = {}
ENTRIES: List[Dict] = []
SCHEDULE_STATE: Dict[str, int] = {}
TEMP_ENTRY: Dict[int, Dict] = {}

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
BLOCK_STATUSES = {"active", "interviewed"}

def user_has_blocking_entries(user_id: int) -> bool:
    return any(
        e.get("user_id") == int(user_id) and e.get("status", "active") in BLOCK_STATUSES
        for e in ENTRIES
    )

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
        if "other" in values and len(values) > 1:
            return await interaction.response.send_message("「その他」は単独で選択してください。", ephemeral=True)

        if "other" in values:
            return await interaction.response.send_modal(CustomTimeModal())

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
    msg = await channel.send(embed=_build_schedule_embed(), allowed_mentions=discord.AllowedMentions.none())
    SCHEDULE_STATE["schedule_channel_id"] = channel.id
    SCHEDULE_STATE["message_id"] = msg.id
    save_schedule_state()
    return msg

async def update_schedule_panel():
    msg = await ensure_schedule_message()
    if msg:
        await msg.edit(embed=_build_schedule_embed(), allowed_mentions=discord.AllowedMentions.none())

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
#  パネル送信
# ==========================
class EntryStatusControlView(discord.ui.View):
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
            uid = entry.get("user_id")
            for e in ENTRIES:
                if e.get("user_id") == uid and e.get("status", "active") == "active":
                    e["status"] = "interviewed"
            save_entries()
        elif status_key == "no_response":
            entry["status"] = "no_response"
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

    embed = discord.Embed(title="入社エントリー", description="以下の内容で受付しました。", color=discord.Color.blue())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="お名前", value=data["name"], inline=False)
    embed.add_field(name="入社日程", value=" / ".join(
        data["custom_time"] if val == "other" else lbl for lbl, val in zip(chosen_labels, chosen_values)
    ), inline=False)
    embed.add_field(name="紹介者", value=data["referrer"], inline=False)

    target_channel = interaction.channel or (await user.create_dm())
    sent_msg = await target_channel.send(embed=embed, view=EntryStatusControlView())

    guild_id = interaction.guild.id if interaction.guild else 0
    for val in chosen_values:
        add_entry_record(
            guild_id, sent_msg.channel.id, sent_msg.id,
            user.id, data["name"], data["referrer"], val,
            data.get("custom_time") if val == "other" else None
        )

    await interaction.response.send_message("送信しました。ありがとうございます！", ephemeral=True)
    if isinstance(target_channel, discord.TextChannel):
        await update_schedule_panel()

# ==========================
#  コマンド
# ==========================
@tree.command(description="予定表を手動で更新")
async def schedule_refresh(interaction: discord.Interaction):
    await update_schedule_panel()
    await interaction.response.send_message("予定表を更新しました。", ephemeral=True)

@tree.command(description="疎通確認")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")

@tree.command(description="エントリー削除（管理者用）")
@app_commands.describe(message_id="エントリーパネルのメッセージID", slot="時間帯を指定するとその枠のみ削除")
@app_commands.choices(slot=[app_commands.Choice(name=label, value=value) for (label, value) in SLOT_ORDER])
async def entry_delete(interaction: discord.Interaction, message_id: str, slot: Optional[app_commands.Choice[str]] = None):
    if not isinstance(interaction.user, discord.Member) or not any(r.id == ENTRY_MANAGER_ROLE_ID for r in interaction.user.roles):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_idは数値で指定してください。", ephemeral=True)
    slot_key = slot.value if slot else None
    count = 0
    for e in ENTRIES:
        if e.get("message_id") == mid and e.get("status") == "active":
            if slot_key is None or e.get("slot_key") == slot_key:
                e["status"] = "deleted"
                count += 1
    if count:
        save_entries()
        await update_schedule_panel()
    await interaction.response.send_message(f"{count}件削除しました。", ephemeral=True)

@tree.command(description="Botに発言させます（管理者用）")
@app_commands.describe(content="本文", channel="送信先（未指定なら今のチャンネル）", allow_mentions="メンションを有効化")
async def say(interaction: discord.Interaction, content: str, channel: Optional[discord.TextChannel] = None, allow_mentions: Optional[bool] = False):
    if not isinstance(interaction.user, discord.Member) or not any(r
