import os
import logging
import asyncio
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
intents.message_content = True  # メッセージ参照が必要ならON
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# 一時保存（ユーザーごとの入力途中データ）
# { user_id: {"name": str, "referrer": str, "custom_time": Optional[str]} }
TEMP_ENTRY: dict[int, dict] = {}

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


# ==========================
#  モーダル各種
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
        placeholder="例）佐藤 花子（いなければ「なし」）",
        required=True,
        max_length=50
    )

    def __init__(self, opener_message_id: int | None = None):
        super().__init__(timeout=None)
        self.opener_message_id = opener_message_id

    async def on_submit(self, interaction: discord.Interaction):
        # 一時保存
        TEMP_ENTRY[interaction.user.id] = {
            "name": str(self.name),
            "referrer": str(self.referrer),
            "custom_time": None,  # 「その他」用の自由入力は後段で扱う
        }
        # 続けて入社日程のセレクトを出す（エフェメラル）
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
            await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
            return

        data["custom_time"] = str(self.custom_time)
        await post_panel_and_confirm(interaction, chosen_label="その他", chosen_value="other")


# ==========================
#  セレクト・ボタンビュー
# ==========================
class TimeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=value)
            for (label, value) in TIME_OPTIONS
        ]
        super().__init__(placeholder="入社日程を選んでください", min_values=1, max_values=1, options=options, custom_id="select_join_time")

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        label = next((lbl for lbl, val in TIME_OPTIONS if val == value), value)

        if value == "other":
            # 自由入力モーダルを開く
            await interaction.response.send_modal(CustomTimeModal())
        else:
            # そのままパネル投稿
            await post_panel_and_confirm(interaction, chosen_label=label, chosen_value=value)


class TimeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(TimeSelect())


class EntryButtonView(discord.ui.View):
    # 永続ビュー（再起動後もボタンが生き続ける）
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="入社日程記入",
        style=discord.ButtonStyle.primary,
        custom_id="entry_button_open_modal"
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 名前・紹介者のモーダルを開く
        await interaction.response.send_modal(BasicInfoModal())


# ==========================
#  パネル投稿（Embed）共通処理
# ==========================
async def post_panel_and_confirm(interaction: discord.Interaction, chosen_label: str, chosen_value: str):
    user = interaction.user
    data = TEMP_ENTRY.get(user.id)
    if not data:
        await interaction.response.send_message("入力セッションが見つかりません。最初からやり直してください。", ephemeral=True)
        return

    # 入社日程の表示テキストを確定
    if chosen_value == "other":
        schedule_text = data.get("custom_time") or "（自由入力なし）"
    else:
        schedule_text = chosen_label

    # Embed（パネル）作成
    embed = discord.Embed(
        title="入社エントリー",
        color=discord.Color.blue(),
        description="以下の内容で受付しました。"
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="お名前", value=data["name"], inline=False)
    embed.add_field(name="入社日程", value=schedule_text, inline=False)
    embed.add_field(name="紹介者", value=data["referrer"], inline=False)
    embed.add_field(name="Discord ID", value=str(user.id), inline=False)
    embed.set_footer(text=f"送信者: {user.display_name}")

    # チャンネル（最初のコマンドを打った場所）に投稿…ではなく、
    # 今回はボタンを押した「同じチャンネル」に投稿する
    # interaction.channel はNoneの可能性が低いが、一応ガード
    target_channel = interaction.channel or (await user.create_dm())
    await target_channel.send(embed=embed)

    # 後始末＆本人にエフェメラルで通知
    TEMP_ENTRY.pop(user.id, None)
    await interaction.followup.send("送信しました。ありがとうございます！", ephemeral=True)


# ==========================
#  Slash コマンド
# ==========================
@tree.command(description="入社日程案内のパネルを設置します")
async def entry_panel(interaction: discord.Interaction):
    """
    実行したテキストチャンネルに、案内メッセージ＋「入社日程記入」ボタンを送信。
    ボタンは永続（再起動後も有効）です。
    """
    view = EntryButtonView()
    msg = "やあ、よく来たね。入社日程について話そう"
    await interaction.response.send_message(msg, view=view)


@tree.command(description="疎通確認（/ping）")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")


# ==========================
#  起動時処理
# ==========================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # 永続ビューの再登録（過去メッセージ上のボタンを有効化）
    bot.add_view(EntryButtonView())

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
    # RailwayのWebポートを開ける（ヘルスチェック用）
    try:
        from keep_alive import run_server
        asyncio.get_event_loop().create_task(run_server())
    except Exception:
        log.warning("keep_alive サーバーは起動しませんでした（ローカルなど）")
    main()
