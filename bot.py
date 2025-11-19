import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones
import asyncio
from pymongo.errors import ServerSelectionTimeoutError

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import MongoClient
from bson.objectid import ObjectId
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

LOG = logging.getLogger('todo-bot')
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
DB_NAME = os.getenv('MONGO_DB', 'todo_bot')
DEFAULT_TZ = os.getenv('DEFAULT_TZ', 'Asia/Jakarta')

if not TOKEN or not MONGO_URI:
    LOG.error('Missing DISCORD_TOKEN or MONGO_URI in environment')
    raise SystemExit('Please set DISCORD_TOKEN and MONGO_URI in environment or .env')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# Mongo
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
tasks_col = db['tasks']
users_col = db['user_settings']
guilds_col = db['guild_settings']

# Scheduler
scheduler = AsyncIOScheduler()

# Reminder thresholds in hours
REMINDER_THRESHOLDS = [72, 24, 5]


def parse_deadline(date_str: str, tz_str: str) -> datetime:
    """Parse a datetime string 'YYYY-MM-DD HH:MM' in user's timezone and return a datetime
    normalized to the default timezone (DEFAULT_TZ, Asia/Jakarta by default)."""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
    except ValueError:
        raise ValueError("Format harus: YYYY-MM-DD HH:MM")

    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        raise ValueError(f"Timezone tidak dikenali: {tz_str}")

    local_dt = dt.replace(tzinfo=tz)
    # Convert to UTC for storage (PyMongo stores times in UTC). We'll display
    # and compute reminders relative to DEFAULT_TZ, but store as UTC to avoid
    # ambiguity with MongoDB behavior.
    utc_dt = local_dt.astimezone(ZoneInfo('UTC'))
    return utc_dt


def ensure_aware_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in UTC.

    PyMongo returns naive datetimes (no tzinfo). Treat naive datetimes as UTC and
    return an aware datetime in UTC so arithmetic with aware datetimes works.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo('UTC'))
    return dt.astimezone(ZoneInfo('UTC'))


def human_delta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return 'sudah lewat'
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return ' '.join(parts)


async def get_user_timezone(user_id: int) -> str:
    doc = users_col.find_one({'user_id': user_id})
    return doc.get('timezone', DEFAULT_TZ) if doc else DEFAULT_TZ


def ensure_aware_tz(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in the default timezone.

    PyMongo returns naive datetimes (no tzinfo). Treat naive datetimes as in
    the default timezone (Asia/Jakarta by default) and return an aware datetime
    in that timezone so arithmetic with aware datetimes works.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
    return dt.astimezone(ZoneInfo(DEFAULT_TZ))


@bot.event
async def on_ready():
    LOG.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    # Sync application commands (slash commands).
    # If you want fast registration for testing, set DEV_GUILDS env to a comma-separated list of guild IDs.
    try:
        dev_guilds = os.getenv('DEV_GUILDS')
        if dev_guilds:
            gids = [g.strip() for g in dev_guilds.split(',') if g.strip()]
            for gid in gids:
                try:
                    obj = discord.Object(id=int(gid))
                    await tree.sync(guild=obj)
                    LOG.info('Synced commands to guild %s', gid)
                except Exception:
                    LOG.exception('Failed to sync commands to guild %s', gid)
        else:
            # Global sync (may take up to an hour to appear on Discord)
            try:
                await tree.sync()
                LOG.info('Globally synced application commands (may take up to 1 hour to appear)')
            except Exception:
                LOG.exception('Failed to globally sync application commands')

        scheduler.start()
        scheduler.add_job(check_reminders, IntervalTrigger(minutes=1))
        # Add weekly summary job: every Sunday at 20:00 (8 PM)
        scheduler.add_job(
            send_weekly_summary,
            CronTrigger(day_of_week=6, hour=20, minute=0, timezone=DEFAULT_TZ)
        )
        LOG.info('Scheduler started (1-minute interval + weekly summary on Sundays)')
    except Exception as e:
        LOG.exception('Failed to start scheduler or sync commands: %s', e)


@tree.command(name='settimezone', description='Atur timezone untuk tampilan waktu reminder')
@app_commands.describe(tz='Nama timezone IANA, contoh: Asia/Jakarta atau UTC')
async def settimezone(interaction: discord.Interaction, tz: str):
    await interaction.response.defer(ephemeral=True)
    if tz not in available_timezones():
        embed = discord.Embed(
            title='❌ Timezone Tidak Valid',
            description=f'Timezone `{tz}` tidak dikenali.\n\nContoh timezone yang valid: `Asia/Jakarta`, `UTC`, `America/New_York`',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    users_col.update_one({'user_id': interaction.user.id}, {'$set': {'timezone': tz}}, upsert=True)
    embed = discord.Embed(
        title='🌐 Timezone Berhasil Diatur',
        description=f'Timezone kamu sekarang: **{tz}**\n\nSemua waktu deadline akan ditampilkan dalam timezone ini.',
        color=discord.Color.blue()
    )
    embed.set_footer(text=f'Diatur oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.now(ZoneInfo('UTC'))
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name='setchannel', description='Atur channel untuk reminder di server ini (butuh izin Manage Server)')
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel='Channel teks untuk mengirim reminder')
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.followup.send('Perintah ini hanya bisa dipakai di dalam server.', ephemeral=True)
        return
    # store channel id in a simple guild_settings collection
    db['guild_settings'].update_one({'guild_id': guild_id}, {'$set': {'channel_id': channel.id}}, upsert=True)
    embed = discord.Embed(
        title='📢 Channel Reminder Diatur',
        description=f'Semua reminder untuk server ini akan dikirim ke {channel.mention}',
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
    embed.set_footer(text=f'Diatur oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.now(ZoneInfo('UTC'))
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name='ping', description='Cek latency dan status koneksi bot')
async def ping(interaction: discord.Interaction):
    """Simple ping command to check latency."""
    ms = round(bot.latency * 1000)
    
    # Color based on latency
    if ms < 100:
        color = discord.Color.green()
        status = '🟢 Excellent'
    elif ms < 200:
        color = discord.Color.gold()
        status = '🟡 Good'
    else:
        color = discord.Color.red()
        status = '🔴 Slow'
    
    embed = discord.Embed(
        title='🏓 Pong!',
        description=f'**Latency:** {ms}ms\n**Status:** {status}',
        color=color
    )
    embed.set_footer(text=f'Requested by {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.now(ZoneInfo('UTC'))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='help', description='Tampilkan bantuan singkat tentang perintah bot')
async def help_cmd(interaction: discord.Interaction):
    """Show help embed with command summaries."""
    embed = discord.Embed(
        title='📋 To-Do Bot — Help Center',
        description='Kelola tugas kamu dengan mudah menggunakan command-command berikut:',
        color=discord.Color.blurple()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url if bot.user else None)
    embed.add_field(
        name='✅ /add',
        value='Tambah tugas baru\n`judul` `tanggal_deadline` `deskripsi` `tag`\nFormat: YYYY-MM-DD HH:MM\nTag: 👤 Individu (default) atau 👥 Kelompok',
        inline=False
    )
    embed.add_field(
        name='📝 /list',
        value='Tampilkan semua tugas yang belum selesai\nUrut berdasarkan deadline terdekat',
        inline=False
    )
    embed.add_field(
        name='✏️ /edit',
        value='Edit tugas yang sudah ada\nUbah judul, deadline, deskripsi, atau tag\nGunakan ID atau nomor dari /list',
        inline=False
    )
    embed.add_field(
        name='✔️ /done',
        value='Tandai tugas selesai\nGunakan ID (8 karakter) atau nomor dari /list',
        inline=False
    )
    embed.add_field(
        name='👥 /assign',
        value='Assign tugas kelompok ke user lain\nMention user yang ingin di-assign\nGunakan ID atau nomor dari /list',
        inline=False
    )
    embed.add_field(
        name='📊 /listkelompok',
        value='Tampilkan semua tugas kelompok di server\nMelihat siapa yang di-assign dan status tugas',
        inline=False
    )
    embed.add_field(
        name='⏰ /setreminder',
        value='Set custom reminder untuk tugas\nFormat: "1d" (1 hari), "3h" (3 jam), "30m" (30 menit)\nAtau: YYYY-MM-DD HH:MM',
        inline=False
    )
    embed.add_field(
        name='🌐 /settimezone',
        value='Set timezone untuk tampilan waktu\nContoh: Asia/Jakarta, UTC, America/New_York',
        inline=False
    )
    embed.add_field(
        name='📢 /setchannel',
        value='Set channel untuk reminder (perlu Manage Server)\nReminder akan muncul di channel yang dipilih',
        inline=False
    )
    embed.add_field(
        name='🏓 /ping',
        value='Cek latency dan status koneksi bot',
        inline=False
    )
    embed.set_footer(text=f'Default timezone: {DEFAULT_TZ} • Gunakan /settimezone untuk mengganti')
    embed.timestamp = datetime.now(ZoneInfo('UTC'))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='add', description='Tambah tugas ke to-do list')
@app_commands.describe(
    judul='Judul tugas',
    tanggal_deadline='Format: YYYY-MM-DD HH:MM',
    deskripsi='Deskripsi (opsional)',
    tag='Tag tugas: individu atau kelompok (default: individu)'
)
@app_commands.choices(tag=[
    app_commands.Choice(name='👤 Individu', value='individu'),
    app_commands.Choice(name='👥 Kelompok', value='kelompok')
])
async def add(interaction: discord.Interaction, judul: str, tanggal_deadline: str, deskripsi: str = None, tag: str = 'individu'):
    await interaction.response.defer()
    tz = await get_user_timezone(interaction.user.id)
    try:
        deadline_utc = parse_deadline(tanggal_deadline, tz)
    except ValueError as e:
        await interaction.followup.send(f'Error: {e}', ephemeral=True)
        return

    now_utc = datetime.now(ZoneInfo('UTC'))
    if deadline_utc <= now_utc:
        await interaction.followup.send('Deadline tidak boleh di masa lalu.', ephemeral=True)
        return

    doc = {
        'user_id': interaction.user.id,
        'guild_id': interaction.guild.id if interaction.guild else None,
        'judul': judul,
        'deskripsi': deskripsi,
        'deadline': deadline_utc,
        'status': False,
        'created_at': now_utc,
        'reminders_sent': [],
        'tag': tag,
        'assigned_users': [],  # For group tasks
        'custom_reminders': []  # For custom reminder times
    }
    res = tasks_col.insert_one(doc)
    short_id = str(res.inserted_id)[:8]
    dl_local = deadline_utc.astimezone(ZoneInfo(tz))
    delta = deadline_utc - now_utc
    countdown = human_delta(delta)
    
    # Tag emoji
    tag_emoji = '👤' if tag == 'individu' else '👥'
    tag_display = f'{tag_emoji} {tag.capitalize()}'
    
    embed = discord.Embed(
        title=f'✅ Tugas Berhasil Ditambahkan',
        description=f'**{judul}**\n\n{deskripsi or "_Tidak ada deskripsi_"}',
        color=discord.Color.brand_green()
    )
    embed.add_field(name='🆔 ID', value=f'`{short_id}`', inline=True)
    embed.add_field(name='🏷️ Tag', value=tag_display, inline=True)
    embed.add_field(name='📅 Deadline', value=dl_local.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
    embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
    embed.add_field(name='👤 Dibuat Oleh', value=f'<@{interaction.user.id}>', inline=True)
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed, ephemeral=True)


# Pagination View untuk /list
class TaskListView(discord.ui.View):
    def __init__(self, tasks, user_id, tz, guild_id):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.tasks = tasks
        self.user_id = user_id
        self.tz = tz
        self.guild_id = guild_id
        self.current_page = 0
        self.items_per_page = 5
        self.total_pages = (len(tasks) - 1) // self.items_per_page + 1
        
        # Update button states
        self.update_buttons()
    
    def update_buttons(self):
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
    
    def get_embed(self):
        now_utc = datetime.now(ZoneInfo('UTC'))
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(self.tasks))
        page_tasks = self.tasks[start_idx:end_idx]
        
        embed = discord.Embed(
            title='📝 Daftar Tugas Aktif',
            description=f'Total: **{len(self.tasks)}** tugas aktif',
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url='https://i.pinimg.com/736x/87/8b/74/878b7473dc8d68673b9ae5f3ca4103ba.jpg')
        
        for idx, t in enumerate(page_tasks, start=start_idx + 1):
            deadline_utc = ensure_aware_utc(t['deadline'])
            dl_local = deadline_utc.astimezone(ZoneInfo(self.tz))
            delta = deadline_utc - now_utc
            countdown = human_delta(delta)
            short_id = str(t['_id'])[:8]
            desc = t.get('deskripsi') or '_Tidak ada deskripsi_'
            
            # Emoji based on urgency
            hours_left = delta.total_seconds() / 3600
            if hours_left < 24:
                emoji = '🔴'
            elif hours_left < 72:
                emoji = '🟡'
            else:
                emoji = '🟢'
            
            # Tag emoji
            task_tag = t.get('tag', 'individu')
            tag_emoji = '👤' if task_tag == 'individu' else '👥'
            
            # Assigned users
            assigned = t.get('assigned_users', [])
            assigned_text = ''
            if assigned:
                assigned_mentions = ' '.join([f'<@{uid}>' for uid in assigned[:3]])
                if len(assigned) > 3:
                    assigned_mentions += f' +{len(assigned) - 3}'
                assigned_text = f'\n👥 **Assigned:** {assigned_mentions}'
            
            field_name = f'{emoji} {idx}. {t["judul"]} {tag_emoji}'
            field_value = (
                f'{desc}'
                f'{assigned_text}\n'
                f'📅 **Deadline:** {dl_local.strftime("%Y-%m-%d %H:%M %Z")}\n'
                f'⏰ **Tersisa:** {countdown}\n'
                f'🆔 `{short_id}`'
            )
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        embed.set_footer(text=f'Halaman {self.current_page + 1}/{self.total_pages}')
        embed.timestamp = now_utc
        return embed
    
    @discord.ui.button(label='◀️ Prev', style=discord.ButtonStyle.gray, custom_id='prev')
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list tugas kamu!', ephemeral=True)
            return
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    
    @discord.ui.button(label='Next ▶️', style=discord.ButtonStyle.gray, custom_id='next')
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list tugas kamu!', ephemeral=True)
            return
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    
    @discord.ui.button(label='🔄 Refresh', style=discord.ButtonStyle.green, custom_id='refresh')
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list tugas kamu!', ephemeral=True)
            return
        # Re-fetch tasks
        cursor = tasks_col.find({'user_id': self.user_id, 'guild_id': self.guild_id, 'status': False}).sort('deadline', 1)
        rows = list(cursor)
        now_utc = datetime.now(ZoneInfo('UTC'))
        self.tasks = [t for t in rows if ensure_aware_utc(t['deadline']) > now_utc]
        self.total_pages = (len(self.tasks) - 1) // self.items_per_page + 1 if self.tasks else 1
        self.current_page = min(self.current_page, self.total_pages - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


@tree.command(name='list', description='Tampilkan list tugas belum selesai')
async def list_tasks(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
    rows = list(cursor)
    
    # Filter out overdue tasks (deadline has passed)
    now_utc = datetime.now(ZoneInfo('UTC'))
    active_rows = []
    for t in rows:
        deadline_utc = ensure_aware_utc(t['deadline'])
        if deadline_utc > now_utc:  # Only include tasks with future deadlines
            active_rows.append(t)
    
    if not active_rows:
        embed = discord.Embed(
            title='📝 Daftar Tugas',
            description='✨ Tidak ada tugas aktif! Gunakan `/add` untuk membuat tugas baru.',
            color=discord.Color.light_grey()
        )
        embed.set_footer(text=f'{interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    tz = await get_user_timezone(user_id)
    
    # Use pagination if more than 5 tasks
    if len(active_rows) > 5:
        view = TaskListView(active_rows, user_id, tz, guild_id)
        await interaction.followup.send(embed=view.get_embed(), view=view)
        return
    
    # Simple embed for <=5 tasks
    embed = discord.Embed(
        title='📝 Daftar Tugas Aktif',
        description=f'Total: **{len(active_rows)}** tugas aktif',
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url='https://i.pinimg.com/736x/87/8b/74/878b7473dc8d68673b9ae5f3ca4103ba.jpg')
    
    for idx, t in enumerate(active_rows, start=1):
        deadline_utc = ensure_aware_utc(t['deadline'])
        dl_local = deadline_utc.astimezone(ZoneInfo(tz))
        delta = deadline_utc - now_utc
        countdown = human_delta(delta)
        short_id = str(t['_id'])[:8]
        desc = t.get('deskripsi') or '_Tidak ada deskripsi_'
        
        # Emoji based on urgency (only for active tasks now)
        hours_left = delta.total_seconds() / 3600
        if hours_left < 24:
            emoji = '🔴'  # urgent
        elif hours_left < 72:
            emoji = '🟡'  # soon
        else:
            emoji = '🟢'  # plenty of time
        
        # Tag emoji
        task_tag = t.get('tag', 'individu')
        tag_emoji = '👤' if task_tag == 'individu' else '👥'
        
        # Assigned users
        assigned = t.get('assigned_users', [])
        assigned_text = ''
        if assigned:
            assigned_mentions = ' '.join([f'<@{uid}>' for uid in assigned[:3]])
            if len(assigned) > 3:
                assigned_mentions += f' +{len(assigned) - 3}'
            assigned_text = f'\n👥 **Assigned:** {assigned_mentions}'
        
        field_name = f'{emoji} {idx}. {t["judul"]} {tag_emoji}'
        field_value = (
            f'{desc}'
            f'{assigned_text}\n'
            f'📅 **Deadline:** {dl_local.strftime("%Y-%m-%d %H:%M %Z")}\n'
            f'⏰ **Tersisa:** {countdown}\n'
            f'🆔 `{short_id}`'
        )
        embed.add_field(name=field_name, value=field_value, inline=False)

    embed.set_footer(text=f'Requested by {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed)


@tree.command(name='edit', description='Edit tugas yang sudah ada')
@app_commands.describe(
    identifier='ID tugas (8 karakter) atau nomor urut dari /list',
    judul='Judul baru (kosongkan jika tidak ingin diubah)',
    tanggal_deadline='Deadline baru format YYYY-MM-DD HH:MM (kosongkan jika tidak ingin diubah)',
    deskripsi='Deskripsi baru (kosongkan jika tidak ingin diubah)',
    tag='Tag tugas: individu atau kelompok (kosongkan jika tidak ingin diubah)'
)
@app_commands.choices(tag=[
    app_commands.Choice(name='👤 Individu', value='individu'),
    app_commands.Choice(name='👥 Kelompok', value='kelompok')
])
async def edit(interaction: discord.Interaction, identifier: str, judul: str = None, tanggal_deadline: str = None, deskripsi: str = None, tag: str = None):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    
    # Check if at least one field is provided for update
    if not judul and not tanggal_deadline and not deskripsi and not tag:
        embed = discord.Embed(
            title='⚠️ Tidak Ada Perubahan',
            description='Kamu harus memberikan minimal satu field untuk diubah:\n• `judul`\n• `tanggal_deadline`\n• `deskripsi`\n• `tag`',
            color=discord.Color.orange()
        )
        embed.set_footer(text=f'Diminta oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Find the target task (same logic as /done)
    target = None
    if identifier.isdigit():
        idx = int(identifier) - 1
        cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
        rows = list(cursor)
        
        # Filter out overdue tasks
        now_utc = datetime.now(ZoneInfo('UTC'))
        active_rows = []
        for t in rows:
            deadline_utc = ensure_aware_utc(t['deadline'])
            if deadline_utc > now_utc:
                active_rows.append(t)
        
        if 0 <= idx < len(active_rows):
            target = active_rows[idx]
    else:
        # try match by prefix or full ObjectId
        try:
            candidate = tasks_col.find_one({'_id': ObjectId(identifier)})
            if candidate and candidate['user_id'] == user_id and candidate.get('guild_id') == guild_id:
                target = candidate
        except Exception:
            # try prefix match
            cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False})
            for r in cursor:
                if str(r['_id']).startswith(identifier):
                    target = r
                    break

    if not target:
        embed = discord.Embed(
            title='❌ Tugas Tidak Ditemukan',
            description=f'Tidak dapat menemukan tugas dengan identifier: `{identifier}`\n\n💡 **Tips:**\n• Gunakan `/list` untuk melihat nomor atau ID tugas\n• ID harus minimal 8 karakter pertama\n• Pastikan tugas milik kamu dan belum selesai',
            color=discord.Color.red()
        )
        embed.set_thumbnail(url='https://i.pinimg.com/736x/9c/6d/96/9c6d96bb7d1e10e4ce0de2914329be36.jpg')
        embed.set_footer(text=f'Diminta oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        embed.timestamp = datetime.now(ZoneInfo('UTC'))
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Build update document
    update_doc = {}
    changes = []
    
    # Get user timezone
    tz = await get_user_timezone(user_id)
    
    # Update judul if provided
    if judul:
        update_doc['judul'] = judul
        changes.append(f"**Judul:** `{target['judul']}` → `{judul}`")
    
    # Update deadline if provided
    if tanggal_deadline:
        try:
            new_deadline_utc = parse_deadline(tanggal_deadline, tz)
            now_utc = datetime.now(ZoneInfo('UTC'))
            
            if new_deadline_utc <= now_utc:
                embed = discord.Embed(
                    title='❌ Deadline Tidak Valid',
                    description='Deadline tidak boleh di masa lalu.',
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            update_doc['deadline'] = new_deadline_utc
            update_doc['reminders_sent'] = []  # Reset reminders when deadline changes
            
            old_dl = ensure_aware_utc(target['deadline']).astimezone(ZoneInfo(tz))
            new_dl = new_deadline_utc.astimezone(ZoneInfo(tz))
            changes.append(f"**Deadline:** `{old_dl.strftime('%Y-%m-%d %H:%M')}` → `{new_dl.strftime('%Y-%m-%d %H:%M')}`")
        except ValueError as e:
            embed = discord.Embed(
                title='❌ Format Deadline Salah',
                description=f'Error: {e}\n\nFormat yang benar: `YYYY-MM-DD HH:MM`\nContoh: `2025-11-15 14:30`',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
    
    # Update deskripsi if provided
    if deskripsi:
        update_doc['deskripsi'] = deskripsi
        old_desc = target.get('deskripsi') or '_Kosong_'
        changes.append(f"**Deskripsi:** `{old_desc[:50]}...` → `{deskripsi[:50]}...`")
    
    # Update tag if provided
    if tag:
        update_doc['tag'] = tag
        old_tag = target.get('tag', 'individu')
        old_tag_emoji = '👤' if old_tag == 'individu' else '👥'
        new_tag_emoji = '👤' if tag == 'individu' else '👥'
        changes.append(f"**Tag:** `{old_tag_emoji} {old_tag.capitalize()}` → `{new_tag_emoji} {tag.capitalize()}`")
    
    # Update the task in database
    tasks_col.update_one({'_id': target['_id']}, {'$set': update_doc})
    
    # Create success embed
    embed = discord.Embed(
        title='✏️ Tugas Berhasil Diubah',
        description=f"**{update_doc.get('judul', target['judul'])}**",
        color=discord.Color.blue()
    )
    
    # Show what changed
    embed.add_field(
        name='📝 Perubahan',
        value='\n'.join(changes),
        inline=False
    )
    
    # Show current state after edit
    final_deadline = ensure_aware_utc(update_doc.get('deadline', target['deadline'])).astimezone(ZoneInfo(tz))
    now_utc = datetime.now(ZoneInfo('UTC'))
    delta = ensure_aware_utc(update_doc.get('deadline', target['deadline'])) - now_utc
    countdown = human_delta(delta)
    
    embed.add_field(name='📅 Deadline Sekarang', value=final_deadline.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
    embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
    embed.add_field(name='👤 Diedit Oleh', value=f'<@{user_id}>', inline=True)
    
    embed.set_footer(text=f'ID: {str(target["_id"])[:8]}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = now_utc
    
    await interaction.followup.send(embed=embed)


@tree.command(name='done', description='Tandai tugas selesai dengan ID atau nomor')
@app_commands.describe(identifier='ObjectId (prefix 8 chars ok) atau nomor urut dari /list')
async def done(interaction: discord.Interaction, identifier: str):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None

    # Try as number (index)
    target = None
    if identifier.isdigit():
        idx = int(identifier) - 1
        cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
        rows = list(cursor)
        
        # Filter out overdue tasks - same logic as /list
        now_utc = datetime.now(ZoneInfo('UTC'))
        active_rows = []
        for t in rows:
            deadline_utc = ensure_aware_utc(t['deadline'])
            if deadline_utc > now_utc:  # Only include tasks with future deadlines
                active_rows.append(t)
        
        if 0 <= idx < len(active_rows):
            target = active_rows[idx]
    else:
        # try match by prefix or full ObjectId
        try:
            # allow short prefix
            candidate = tasks_col.find_one({'_id': ObjectId(identifier)})
            if candidate and candidate['user_id'] == user_id and candidate.get('guild_id') == guild_id:
                target = candidate
        except Exception:
            # try prefix match
            cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False})
            for r in cursor:
                if str(r['_id']).startswith(identifier):
                    target = r
                    break

    if not target:
        embed = discord.Embed(
            title='❌ Tugas Tidak Ditemukan',
            description=f'Tidak dapat menemukan tugas dengan identifier: `{identifier}`\n\n💡 **Tips:**\n• Gunakan `/list` untuk melihat nomor atau ID tugas\n• ID harus minimal 8 karakter pertama\n• Pastikan tugas milik kamu dan belum selesai',
            color=discord.Color.red()
        )
        embed.set_thumbnail(url='https://i.pinimg.com/736x/9c/6d/96/9c6d96bb7d1e10e4ce0de2914329be36.jpg')
        embed.set_footer(text=f'Diminta oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        embed.timestamp = datetime.now(ZoneInfo('UTC'))
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Mark as completed
    now_utc = datetime.now(ZoneInfo('UTC'))
    tasks_col.update_one({'_id': target['_id']}, {'$set': {'status': True, 'completed_at': now_utc}})
    
    # Get user timezone for display
    tz = await get_user_timezone(user_id)
    deadline_utc = ensure_aware_utc(target['deadline'])
    dl_local = deadline_utc.astimezone(ZoneInfo(tz))
    created_utc = ensure_aware_utc(target.get('created_at', now_utc))
    
    # Calculate if task was completed on time or late
    time_diff = (now_utc - deadline_utc).total_seconds()
    if time_diff < 0:
        # Completed before deadline
        status_emoji = '🎉'
        status_text = '**Selesai Tepat Waktu!**'
        status_color = discord.Color.dark_green()
        time_status = f'Diselesaikan **{human_delta(timedelta(seconds=abs(time_diff)))}** sebelum deadline'
    else:
        # Completed after deadline
        status_emoji = '⏰'
        status_text = '**Selesai Terlambat**'
        status_color = discord.Color.orange()
        time_status = f'Diselesaikan **{human_delta(timedelta(seconds=time_diff))}** setelah deadline'
    
    # Get tag info
    task_tag = target.get('tag', 'individu')
    tag_emoji = '👤' if task_tag == 'individu' else '👥'
    tag_display = f'{tag_emoji} {task_tag.capitalize()}'
    
    # Create celebration/completion embed
    embed = discord.Embed(
        title=f'{status_emoji} Tugas Diselesaikan!',
        description=f'{status_text}\n\n**{target["judul"]}** {tag_emoji}\n_{target.get("deskripsi") or "Tidak ada deskripsi"}_',
        color=status_color
    )
    
    # Use a checkmark/celebration thumbnail
    embed.set_thumbnail(url='https://i.pinimg.com/736x/6e/7c/2f/6e7c2f925eff6444fb4d8992f1330768.jpg')
    
    embed.add_field(
        name='🏷️ Tag',
        value=tag_display,
        inline=True
    )
    
    embed.add_field(
        name='📅 Deadline',
        value=f'{dl_local.strftime("%Y-%m-%d %H:%M %Z")}\n{time_status}',
        inline=False
    )
    
    embed.add_field(
        name='⏱️ Durasi Tugas',
        value=f'{human_delta(now_utc - created_utc)}',
        inline=True
    )
    
    embed.add_field(
        name='✅ Diselesaikan',
        value=f'<t:{int(now_utc.timestamp())}:R>',
        inline=True
    )
    
    embed.add_field(
        name='👤 Oleh',
        value=f'<@{user_id}>',
        inline=True
    )
    
    embed.set_footer(text=f'ID: {str(target["_id"])[:8]} • Keep up the good work!', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = now_utc
    
    await interaction.followup.send(embed=embed)


@tree.command(name='assign', description='Assign tugas kelompok ke user lain')
@app_commands.describe(
    identifier='ID tugas (8 karakter) atau nomor urut dari /list',
    users='Mention users yang akan di-assign (pisahkan dengan spasi)'
)
async def assign(interaction: discord.Interaction, identifier: str, users: str):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    
    # Find the target task
    target = None
    if identifier.isdigit():
        idx = int(identifier) - 1
        cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
        rows = list(cursor)
        now_utc = datetime.now(ZoneInfo('UTC'))
        active_rows = [t for t in rows if ensure_aware_utc(t['deadline']) > now_utc]
        if 0 <= idx < len(active_rows):
            target = active_rows[idx]
    else:
        try:
            candidate = tasks_col.find_one({'_id': ObjectId(identifier)})
            if candidate and candidate['user_id'] == user_id and candidate.get('guild_id') == guild_id:
                target = candidate
        except Exception:
            cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False})
            for r in cursor:
                if str(r['_id']).startswith(identifier):
                    target = r
                    break
    
    if not target:
        embed = discord.Embed(
            title='❌ Tugas Tidak Ditemukan',
            description=f'Tidak dapat menemukan tugas dengan identifier: `{identifier}`',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Extract user IDs from mentions
    mentioned_ids = [int(uid) for uid in users.replace('<@', '').replace('>', '').replace('!', '').split() if uid.isdigit()]
    
    if not mentioned_ids:
        embed = discord.Embed(
            title='⚠️ Tidak Ada User',
            description='Kamu harus mention minimal 1 user untuk di-assign.\nContoh: `/assign 1 @user1 @user2`',
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Update task dengan assigned users
    current_assigned = target.get('assigned_users', [])
    new_assigned = list(set(current_assigned + mentioned_ids))
    tasks_col.update_one({'_id': target['_id']}, {'$set': {'assigned_users': new_assigned}})
    
    # Create success embed
    embed = discord.Embed(
        title='👥 Tugas Berhasil Di-Assign',
        description=f'**{target["judul"]}**',
        color=discord.Color.green()
    )
    
    assigned_mentions = ' '.join([f'<@{uid}>' for uid in new_assigned])
    embed.add_field(name='📋 Assigned To', value=assigned_mentions, inline=False)
    embed.add_field(name='🆔 ID', value=f'`{str(target["_id"])[:8]}`', inline=True)
    embed.add_field(name='👤 Owner', value=f'<@{target["user_id"]}>', inline=True)
    
    embed.set_footer(text=f'Assigned by {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.now(ZoneInfo('UTC'))
    
    await interaction.followup.send(embed=embed)


@tree.command(name='listkelompok', description='Tampilkan semua tugas kelompok di server ini')
async def listkelompok(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild.id if interaction.guild else None
    
    if not guild_id:
        await interaction.followup.send('❌ Command ini hanya bisa digunakan di server.', ephemeral=True)
        return
    
    # Find all group tasks in this guild
    cursor = tasks_col.find({'guild_id': guild_id, 'tag': 'kelompok', 'status': False}).sort('deadline', 1)
    rows = list(cursor)
    
    # Filter out overdue
    now_utc = datetime.now(ZoneInfo('UTC'))
    active_rows = [t for t in rows if ensure_aware_utc(t['deadline']) > now_utc]
    
    if not active_rows:
        embed = discord.Embed(
            title='👥 Tugas Kelompok',
            description='✨ Tidak ada tugas kelompok aktif di server ini!',
            color=discord.Color.light_grey()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    user_id = interaction.user.id
    tz = await get_user_timezone(user_id)
    
    embed = discord.Embed(
        title='👥 Daftar Tugas Kelompok',
        description=f'Total: **{len(active_rows)}** tugas kelompok aktif di server ini',
        color=discord.Color.purple()
    )
    
    for idx, t in enumerate(active_rows[:5], start=1):  # Limit to 5
        deadline_utc = ensure_aware_utc(t['deadline'])
        dl_local = deadline_utc.astimezone(ZoneInfo(tz))
        delta = deadline_utc - now_utc
        countdown = human_delta(delta)
        short_id = str(t['_id'])[:8]
        
        hours_left = delta.total_seconds() / 3600
        if hours_left < 24:
            emoji = '🔴'
        elif hours_left < 72:
            emoji = '🟡'
        else:
            emoji = '🟢'
        
        # Assigned users
        assigned = t.get('assigned_users', [])
        assigned_text = ''
        if assigned:
            assigned_mentions = ' '.join([f'<@{uid}>' for uid in assigned[:3]])
            if len(assigned) > 3:
                assigned_mentions += f' +{len(assigned) - 3}'
            assigned_text = f'\n👥 {assigned_mentions}'
        
        field_name = f'{emoji} {idx}. {t["judul"]}'
        field_value = (
            f'👤 **Owner:** <@{t["user_id"]}>'
            f'{assigned_text}\n'
            f'📅 **Deadline:** {dl_local.strftime("%Y-%m-%d %H:%M")}\n'
            f'⏰ **Tersisa:** {countdown} • 🆔 `{short_id}`'
        )
        embed.add_field(name=field_name, value=field_value, inline=False)
    
    if len(active_rows) > 5:
        embed.set_footer(text=f'Menampilkan 5 dari {len(active_rows)} tugas')
    
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed)


@tree.command(name='setreminder', description='Set custom reminder untuk tugas')
@app_commands.describe(
    identifier='ID tugas (8 karakter) atau nomor urut dari /list',
    waktu='Waktu reminder (format: YYYY-MM-DD HH:MM atau durasi seperti 1d, 3h, 30m)'
)
async def setreminder(interaction: discord.Interaction, identifier: str, waktu: str):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    
    # Find the target task
    target = None
    if identifier.isdigit():
        idx = int(identifier) - 1
        cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
        rows = list(cursor)
        now_utc = datetime.now(ZoneInfo('UTC'))
        active_rows = [t for t in rows if ensure_aware_utc(t['deadline']) > now_utc]
        if 0 <= idx < len(active_rows):
            target = active_rows[idx]
    else:
        try:
            candidate = tasks_col.find_one({'_id': ObjectId(identifier)})
            if candidate and candidate['user_id'] == user_id and candidate.get('guild_id') == guild_id:
                target = candidate
        except Exception:
            cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False})
            for r in cursor:
                if str(r['_id']).startswith(identifier):
                    target = r
                    break
    
    if not target:
        embed = discord.Embed(
            title='❌ Tugas Tidak Ditemukan',
            description=f'Tidak dapat menemukan tugas dengan identifier: `{identifier}`',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    tz = await get_user_timezone(user_id)
    now_utc = datetime.now(ZoneInfo('UTC'))
    reminder_time = None
    
    # Parse waktu - try duration format first (1d, 3h, 30m)
    import re
    duration_match = re.match(r'^(\d+)([dhm])$', waktu.lower())
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        if unit == 'd':
            reminder_time = now_utc + timedelta(days=amount)
        elif unit == 'h':
            reminder_time = now_utc + timedelta(hours=amount)
        elif unit == 'm':
            reminder_time = now_utc + timedelta(minutes=amount)
    else:
        # Try absolute time format
        try:
            reminder_time = parse_deadline(waktu, tz)
        except ValueError as e:
            embed = discord.Embed(
                title='❌ Format Waktu Salah',
                description=f'Format waktu tidak valid: `{waktu}`\n\n**Format yang didukung:**\n• Durasi: `1d` (1 hari), `3h` (3 jam), `30m` (30 menit)\n• Absolute: `YYYY-MM-DD HH:MM`',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
    
    if reminder_time <= now_utc:
        embed = discord.Embed(
            title='❌ Waktu Reminder Invalid',
            description='Waktu reminder harus di masa depan!',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    deadline_utc = ensure_aware_utc(target['deadline'])
    if reminder_time >= deadline_utc:
        embed = discord.Embed(
            title='⚠️ Peringatan',
            description='Waktu reminder setelah deadline! Apakah kamu yakin?',
            color=discord.Color.orange()
        )
        # Still allow it, just warn
    
    # Add custom reminder
    custom_reminders = target.get('custom_reminders', [])
    custom_reminders.append({
        'time': reminder_time,
        'sent': False,
        'created_by': user_id
    })
    tasks_col.update_one({'_id': target['_id']}, {'$set': {'custom_reminders': custom_reminders}})
    
    # Success embed
    reminder_local = reminder_time.astimezone(ZoneInfo(tz))
    embed = discord.Embed(
        title='⏰ Custom Reminder Ditambahkan',
        description=f'**{target["judul"]}**',
        color=discord.Color.green()
    )
    embed.add_field(name='🔔 Reminder Waktu', value=reminder_local.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
    embed.add_field(name='⏰ Dalam', value=human_delta(reminder_time - now_utc), inline=True)
    embed.add_field(name='🆔 ID', value=f'`{str(target["_id"])[:8]}`', inline=True)
    embed.timestamp = now_utc
    
    await interaction.followup.send(embed=embed)


async def send_reminder_for_task(tdoc, threshold_hours: int):
    user_id = tdoc['user_id']
    guild_id = tdoc.get('guild_id')
    title = tdoc.get('judul')
    deadline_utc = ensure_aware_utc(tdoc['deadline'])
    # fetch user's timezone from DB in executor to avoid blocking
    loop = asyncio.get_running_loop()
    user_doc = await loop.run_in_executor(None, lambda: users_col.find_one({'user_id': user_id}))
    tz = (user_doc or {}).get('timezone', DEFAULT_TZ)
    dl_local = deadline_utc.astimezone(ZoneInfo(tz))
    delta = deadline_utc - datetime.now(ZoneInfo('UTC'))
    countdown = human_delta(delta)

    # Emoji and color based on urgency
    if threshold_hours == 0:
        emoji = '🚨'
        color = discord.Color.dark_red()
        urgency = 'DEADLINE TERCAPAI!'
    elif threshold_hours <= 5:
        emoji = '⚠️'
        color = discord.Color.red()
        urgency = f'Reminder {threshold_hours} Jam!'
    elif threshold_hours <= 24:
        emoji = '⏰'
        color = discord.Color.orange()
        urgency = f'Reminder {threshold_hours} Jam!'
    else:
        emoji = '📢'
        color = discord.Color.gold()
        urgency = f'Reminder {threshold_hours} Jam!'

    embed = discord.Embed(
        title=f'{emoji} {urgency}',
        description=f'**{title}**\n\n{tdoc.get("deskripsi") or "_Tidak ada deskripsi_"}',
        color=color
    )
    embed.add_field(name='📅 Deadline', value=dl_local.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
    embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
    embed.add_field(name='👤 Pembuat', value=f'<@{user_id}>', inline=True)
    embed.set_footer(text=f'ID: {str(tdoc["_id"])[:8]} • Gunakan /done untuk menyelesaikan')
    embed.timestamp = datetime.now(ZoneInfo('UTC'))

    if not guild_id:
        LOG.warning('Task %s has no guild_id, skipping reminder (no guild target).', tdoc['_id'])
        return

    # Prefer configured channel in guild_settings
    # get guild settings (run in executor to avoid blocking)
    loop = asyncio.get_running_loop()
    gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
    channel = None
    if gs and gs.get('channel_id'):
        channel = bot.get_channel(int(gs['channel_id']))

    # fallback: guild.system_channel or first accessible text channel
    if channel is None:
        g = bot.get_guild(guild_id)
        if not g:
            LOG.debug('Guild %s not in cache; skipping reminder for task %s', guild_id, tdoc['_id'])
            return
        channel = g.system_channel
        if channel is None:
            for c in g.channels:
                if isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages:
                    channel = c
                    break

    if channel is None:
        LOG.warning('No available channel to send reminder for guild %s (task %s)', guild_id, tdoc['_id'])
        return

    try:
        # Mention the user in the message and include the embed with details
        await channel.send(content=f'<@{user_id}>', embed=embed)
    except Exception:
        LOG.exception('Failed to send reminder in channel %s for task %s', getattr(channel, 'id', None), tdoc['_id'])


def mark_reminder_sent(task_id, label):
    # synchronous helper still available
    tasks_col.update_one({'_id': task_id}, {'$addToSet': {'reminders_sent': label}})


async def mark_reminder_sent_async(task_id, label):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: tasks_col.update_one({'_id': task_id}, {'$addToSet': {'reminders_sent': label}}))


async def check_reminders():
    now = datetime.now(ZoneInfo(DEFAULT_TZ))
    # Fetch all active tasks and compute time-to-deadline in Python after normalizing
    # datetimes to the DEFAULT_TZ timezone. This allows us to send a 'due' notification
    # when the deadline arrives, as well as pre-deadline reminders.
    # Run DB query in executor to avoid blocking the event loop if Mongo is slow/unreachable
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(None, lambda: list(tasks_col.find({'status': False})))
    except ServerSelectionTimeoutError as e:
        LOG.exception('MongoDB ServerSelectionTimeoutError in check_reminders: %s', e)
        return

    for tdoc in rows:
        # DB stores deadlines in UTC; normalize to aware UTC and then convert
        # to DEFAULT_TZ for comparison and display.
        deadline_utc = ensure_aware_utc(tdoc['deadline'])
        deadline = deadline_utc.astimezone(ZoneInfo(DEFAULT_TZ))
        seconds_left = (deadline - now).total_seconds()

        # Pre-deadline reminders (72h, 24h, 5h)
        for th in REMINDER_THRESHOLDS:
            label = f'rem_{th}h'
            if label in tdoc.get('reminders_sent', []):
                continue
            # check if we are within +/- 90 seconds of the threshold (scheduler runs each minute)
            target_seconds = th * 3600
            if target_seconds - 90 <= seconds_left <= target_seconds + 90:
                try:
                    await send_reminder_for_task(tdoc, th)
                    await mark_reminder_sent_async(tdoc['_id'], label)
                    LOG.info('Sent %sh reminder for task %s', th, tdoc['_id'])
                except Exception:
                    LOG.exception('Failed to send reminder for task %s', tdoc['_id'])

        # Due notification: send once when deadline has passed or is now
        if seconds_left <= 0 and 'rem_due' not in tdoc.get('reminders_sent', []):
            try:
                await send_reminder_for_task(tdoc, 0)
                await mark_reminder_sent_async(tdoc['_id'], 'rem_due')
                LOG.info('Sent due reminder for task %s', tdoc['_id'])
            except Exception:
                LOG.exception('Failed to send due reminder for task %s', tdoc['_id'])
        
        # Auto-delete tasks that are overdue by more than 24 hours
        # This keeps the list clean and removes tasks that should have been completed
        if seconds_left < -86400:  # 24 hours in seconds
            try:
                await loop.run_in_executor(None, lambda: tasks_col.delete_one({'_id': tdoc['_id']}))
                LOG.info('Auto-deleted overdue task %s (overdue by %.1f hours)', tdoc['_id'], abs(seconds_left / 3600))
            except Exception:
                LOG.exception('Failed to auto-delete overdue task %s', tdoc['_id'])
        
        # Custom reminders checking
        custom_reminders = tdoc.get('custom_reminders', [])
        if custom_reminders:
            now_utc = datetime.now(ZoneInfo('UTC'))
            for idx, reminder in enumerate(custom_reminders):
                if reminder.get('sent', False):
                    continue
                reminder_time = ensure_aware_utc(reminder['time'])
                # Check if reminder time has passed (within 90 second window)
                diff = (now_utc - reminder_time).total_seconds()
                if -90 <= diff <= 90:
                    try:
                        # Send custom reminder notification
                        user_id = tdoc['user_id']
                        guild_id = tdoc.get('guild_id')
                        title = tdoc.get('judul')
                        
                        # Get user timezone
                        user_doc = await loop.run_in_executor(None, lambda: users_col.find_one({'user_id': user_id}))
                        tz = (user_doc or {}).get('timezone', DEFAULT_TZ)
                        reminder_local = reminder_time.astimezone(ZoneInfo(tz))
                        dl_local = deadline_utc.astimezone(ZoneInfo(tz))
                        delta = deadline_utc - now_utc
                        countdown = human_delta(delta)
                        
                        embed = discord.Embed(
                            title='🔔 Custom Reminder!',
                            description=f'**{title}**\n\n{tdoc.get("deskripsi") or "_Tidak ada deskripsi_"}',
                            color=discord.Color.blue()
                        )
                        embed.add_field(name='📅 Deadline', value=dl_local.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
                        embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
                        embed.add_field(name='🔔 Reminder Diatur', value=reminder_local.strftime('%Y-%m-%d %H:%M'), inline=True)
                        embed.set_footer(text=f'ID: {str(tdoc["_id"])[:8]} • Gunakan /done untuk menyelesaikan')
                        embed.timestamp = now_utc
                        
                        # Send to configured channel
                        gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
                        channel = None
                        if gs and gs.get('channel_id'):
                            channel = bot.get_channel(int(gs['channel_id']))
                        
                        if channel is None:
                            g = bot.get_guild(guild_id)
                            if g:
                                channel = g.system_channel
                                if channel is None:
                                    for c in g.channels:
                                        if isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages:
                                            channel = c
                                            break
                        
                        if channel:
                            await channel.send(content=f'<@{user_id}>', embed=embed)
                        
                        # Mark as sent
                        custom_reminders[idx]['sent'] = True
                        await loop.run_in_executor(None, lambda: tasks_col.update_one(
                            {'_id': tdoc['_id']},
                            {'$set': {'custom_reminders': custom_reminders}}
                        ))
                        LOG.info('Sent custom reminder for task %s at %s', tdoc['_id'], reminder_time)
                    except Exception:
                        LOG.exception('Failed to send custom reminder for task %s', tdoc['_id'])


async def send_weekly_summary():
    """
    Send weekly task summary every Sunday.
    Recaps all tasks from Monday to Saturday for each guild.
    """
    now_utc = datetime.now(ZoneInfo('UTC'))
    now_local = now_utc.astimezone(ZoneInfo(DEFAULT_TZ))
    
    # Calculate last week's Monday to Saturday
    days_since_monday = (now_local.weekday() - 0) % 7  # 0 = Monday
    if days_since_monday == 0:
        # Today is Monday, go back to last Monday
        days_since_monday = 7
    
    last_monday = (now_local - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    last_saturday = last_monday + timedelta(days=5)  # Monday + 5 = Saturday
    last_saturday = last_saturday.replace(hour=23, minute=59, second=59)
    
    # Convert to UTC for DB query
    start_utc = last_monday.astimezone(ZoneInfo('UTC'))
    end_utc = last_saturday.astimezone(ZoneInfo('UTC'))
    
    LOG.info('Generating weekly summary for period: %s to %s', start_utc, end_utc)
    
    # Get all tasks with deadline in last Monday-Saturday
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(None, lambda: list(tasks_col.find({
            'deadline': {'$gte': start_utc, '$lte': end_utc}
        })))
    except Exception as e:
        LOG.exception('Failed to fetch tasks for weekly summary: %s', e)
        return
    
    # Group tasks by guild_id
    guild_tasks = {}
    for task in rows:
        guild_id = task.get('guild_id')
        if not guild_id:
            continue
        if guild_id not in guild_tasks:
            guild_tasks[guild_id] = []
        guild_tasks[guild_id].append(task)
    
    # Send summary to each guild
    for guild_id, tasks in guild_tasks.items():
        try:
            # Get guild settings for channel
            gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
            channel = None
            if gs and gs.get('channel_id'):
                channel = bot.get_channel(int(gs['channel_id']))
            
            if channel is None:
                g = bot.get_guild(guild_id)
                if not g:
                    continue
                channel = g.system_channel
                if channel is None:
                    for c in g.channels:
                        if isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages:
                            channel = c
                            break
            
            if not channel:
                LOG.warning('No channel found for guild %s weekly summary', guild_id)
                continue
            
            # Count completed vs total
            completed = sum(1 for t in tasks if t.get('status', False))
            total = len(tasks)
            completion_rate = (completed / total * 100) if total > 0 else 0
            
            # Get unique users
            users = set(t['user_id'] for t in tasks)
            
            embed = discord.Embed(
                title='📊 Rekap Mingguan Tugas',
                description=f'**Periode:** {last_monday.strftime("%d %b")} - {last_saturday.strftime("%d %b %Y")}\n\n'
                           f'Ringkasan tugas yang memiliki deadline minggu ini.',
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name='📈 Statistik',
                value=f'✅ **Selesai:** {completed}/{total}\n'
                      f'📊 **Rate:** {completion_rate:.1f}%\n'
                      f'👥 **Kontributor:** {len(users)} orang',
                inline=False
            )
            
            # List top 5 tasks
            if tasks:
                task_list = []
                for idx, t in enumerate(tasks[:5], start=1):
                    status_emoji = '✅' if t.get('status', False) else '❌'
                    deadline_utc = ensure_aware_utc(t['deadline'])
                    deadline_local = deadline_utc.astimezone(ZoneInfo(DEFAULT_TZ))
                    tag_emoji = '👥' if t.get('tag') == 'kelompok' else '👤'
                    task_list.append(
                        f'{status_emoji} {tag_emoji} **{t["judul"]}** '
                        f'({deadline_local.strftime("%d/%m %H:%M")})'
                    )
                
                embed.add_field(
                    name='📋 Daftar Tugas',
                    value='\n'.join(task_list) if task_list else '_Tidak ada tugas_',
                    inline=False
                )
                
                if len(tasks) > 5:
                    embed.set_footer(text=f'Menampilkan 5 dari {len(tasks)} tugas')
            
            embed.timestamp = now_utc
            
            await channel.send(embed=embed)
            LOG.info('Sent weekly summary to guild %s (%d tasks)', guild_id, total)
        
        except Exception:
            LOG.exception('Failed to send weekly summary to guild %s', guild_id)


if __name__ == '__main__':
    # register commands and run
    try:
        bot.run(TOKEN)
    finally:
        scheduler.shutdown(wait=False)
