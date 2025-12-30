import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones
import asyncio
from pymongo.errors import ServerSelectionTimeoutError

import discord
from discord import app_commands
from discord.ext import commands, tasks
from bson.objectid import ObjectId
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Import from local modules
from config import TOKEN, MONGO_URI, DEFAULT_TZ, REMINDER_THRESHOLDS, DEV_GUILDS
from database import tasks_col, events_col, users_col, guilds_col
from utils import (
    parse_deadline, ensure_aware_utc, ensure_aware_tz,
    human_delta, format_date, get_user_timezone
)
from views import TaskListView, EventListView

LOG = logging.getLogger('todo-bot')
logging.basicConfig(level=logging.INFO)

if not TOKEN or not MONGO_URI:
    LOG.error('Missing DISCORD_TOKEN or MONGO_URI in environment')
    raise SystemExit('Please set DISCORD_TOKEN and MONGO_URI in environment or .env')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# Scheduler
scheduler = AsyncIOScheduler()


@bot.event
async def on_ready():
    LOG.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    # Sync application commands (slash commands).
    try:
        if DEV_GUILDS:
            gids = [g.strip() for g in DEV_GUILDS.split(',') if g.strip()]
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


@tree.command(name='setchannel', description='Atur channel untuk reminder (butuh izin Manage Server)')
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    channel='Channel teks untuk mengirim reminder',
    tag='Tipe item: task (tugas) atau event (acara)'
)
@app_commands.choices(tag=[
    app_commands.Choice(name='📋 Task (Tugas)', value='task'),
    app_commands.Choice(name='📅 Event (Acara)', value='event')
])
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel, tag: str = 'task'):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.followup.send('Perintah ini hanya bisa dipakai di dalam server.', ephemeral=True)
        return
    
    # Store channel id with type tag in guild_settings
    db['guild_settings'].update_one(
        {'guild_id': guild_id}, 
        {'$set': {f'channel_id_{tag}': channel.id}}, 
        upsert=True
    )
    
    tag_display = '📋 Task (Tugas)' if tag == 'task' else '📅 Event (Acara)'
    
    embed = discord.Embed(
        title='📢 Channel Reminder Diatur',
        description=f'Channel untuk reminder **{tag_display}** berhasil diatur',
        color=discord.Color.green()
    )
    embed.add_field(name='🏷️ Tipe', value=tag_display, inline=True)
    embed.add_field(name='📍 Channel', value=channel.mention, inline=True)
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
        name='📅 /addevent',
        value='Tambah event baru (rapat, meeting, dll)\n`judul` `tanggal_mulai` `tanggal_selesai` `deskripsi`\nFormat: YYYY-MM-DD HH:MM',
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
        name='✅ /doneevent',
        value='Tandai event selesai\nGunakan ID (8 karakter) atau nomor dari /list',
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
    embed.add_field(name='📅 Deadline', value=format_date(dl_local), inline=False)
    embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
    embed.add_field(name='👤 Dibuat Oleh', value=f'<@{interaction.user.id}>', inline=True)
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name='addevent', description='Tambah event baru (rapat, meeting, dll)')
@app_commands.describe(
    judul='Judul event',
    tanggal_mulai='Format: YYYY-MM-DD HH:MM',
    tanggal_selesai='Format: YYYY-MM-DD HH:MM',
    deskripsi='Deskripsi (opsional)'
)
async def addevent(interaction: discord.Interaction, judul: str, tanggal_mulai: str, tanggal_selesai: str, deskripsi: str = None):
    await interaction.response.defer()
    tz = await get_user_timezone(interaction.user.id)
    try:
        event_utc = parse_deadline(tanggal_mulai, tz)
    except ValueError as e:
        await interaction.followup.send(f'Error: {e}', ephemeral=True)
        return

    now_utc = datetime.now(ZoneInfo('UTC'))
    if event_utc <= now_utc:
        await interaction.followup.send('Tanggal event tidak boleh di masa lalu.', ephemeral=True)
        return

    # Parse end time (required)
    try:
        end_utc = parse_deadline(tanggal_selesai, tz)
    except ValueError as e:
        await interaction.followup.send(f'Error tanggal selesai: {e}', ephemeral=True)
        return
    
    if end_utc <= event_utc:
        await interaction.followup.send('Tanggal selesai harus setelah tanggal mulai.', ephemeral=True)
        return

    doc = {
        'user_id': interaction.user.id,
        'guild_id': interaction.guild.id if interaction.guild else None,
        'judul': judul,
        'deskripsi': deskripsi,
        'tanggal_mulai': event_utc,
        'status': False,
        'created_at': now_utc,
        'reminders_sent': [],
        'custom_reminders': [],
        'tanggal_selesai': end_utc
    }
    res = events_col.insert_one(doc)
    short_id = str(res.inserted_id)[:8]
    event_local = event_utc.astimezone(ZoneInfo(tz))
    
    # Format end time (always available now)
    end_local = end_utc.astimezone(ZoneInfo(tz))
    end_time_text = f' - {end_local.strftime("%H:%M")}'
    
    embed = discord.Embed(
        title=f'✅ Event Berhasil Ditambahkan',
        description=f'**{judul}**\n\n{deskripsi or "_Tidak ada deskripsi_"}',
        color=discord.Color.orange()
    )
    embed.add_field(name='🆔 ID', value=f'`{short_id}`', inline=True)
    embed.add_field(name='📅 Waktu', value=f'{format_date(event_local)} {event_local.strftime("%H:%M")}{end_time_text}', inline=False)
    embed.add_field(name='👤 Dibuat Oleh', value=f'<@{interaction.user.id}>', inline=True)
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed)


# Pagination View para /list
# (Moved to views.py - imported above)


@tree.command(name='list', description='Tampilkan list tugas belum selesai')
async def list_tasks(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    
    # Fetch tasks
    task_cursor = tasks_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('deadline', 1)
    task_rows = list(task_cursor)
    
    # Filter out overdue items
    now_utc = datetime.now(ZoneInfo('UTC'))
    active_tasks = [t for t in task_rows if ensure_aware_utc(t['deadline']) > now_utc]
    
    tz = await get_user_timezone(user_id)
    
    if not active_tasks:
        embed = discord.Embed(
            title='📝 Daftar Tugas',
            description='✨ Tidak ada tugas aktif!\nGunakan `/add` untuk membuat tugas.',
            color=discord.Color.light_grey()
        )
        embed.set_footer(text=f'{interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Use pagination if more than 5 tasks
    if len(active_tasks) > 5:
        view = TaskListView(active_tasks, user_id, tz, guild_id)
        await interaction.followup.send(embed=view.get_embed(), view=view)
        return
    
    # Simple embed for <=5 tasks
    embed = discord.Embed(
        title='📋 Daftar Tugas',
        description=f'Total: **{len(active_tasks)}** tugas aktif',
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url='https://i.pinimg.com/736x/87/8b/74/878b7473dc8d68673b9ae5f3ca4103ba.jpg')
    
    for idx, t in enumerate(active_tasks, start=1):
        deadline_utc = ensure_aware_utc(t['deadline'])
        dl_local = deadline_utc.astimezone(ZoneInfo(tz))
        delta = deadline_utc - now_utc
        countdown = human_delta(delta)
        short_id = str(t['_id'])[:8]
        desc = t.get('deskripsi') or '_Tidak ada deskripsi_'
        
        hours_left = delta.total_seconds() / 3600
        if hours_left < 24:
            emoji = '🔴'
        elif hours_left < 72:
            emoji = '🟡'
        else:
            emoji = '🟢'
        
        task_tag = t.get('tag', 'individu')
        tag_emoji = '👤' if task_tag == 'individu' else '👥'
        
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
            f'📅 **Deadline:** {format_date(dl_local)}\n'
            f'⏰ **Tersisa:** {countdown}\n'
            f'🆔 `{short_id}`'
        )
        embed.add_field(name=field_name, value=field_value, inline=False)

    embed.set_footer(text=f'Requested by {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
    embed.timestamp = now_utc
    await interaction.followup.send(embed=embed)


@tree.command(name='listevent', description='Tampilkan list event belum selesai')
async def list_events(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None
    
    # Fetch events
    event_cursor = events_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('tanggal_mulai', 1)
    event_rows = list(event_cursor)
    
    # Filter out overdue items
    now_utc = datetime.now(ZoneInfo('UTC'))
    active_events = []
    for e in event_rows:
        # Handle both old 'tanggal' and new 'tanggal_mulai' field names
        event_time = e.get('tanggal_mulai') or e.get('tanggal')
        if event_time and ensure_aware_utc(event_time) > now_utc:
            active_events.append(e)
    
    tz = await get_user_timezone(user_id)
    
    if not active_events:
        embed = discord.Embed(
            title='📅 Daftar Event',
            description='✨ Tidak ada event aktif!\nGunakan `/addevent` untuk membuat event.',
            color=discord.Color.light_grey()
        )
        embed.set_footer(text=f'{interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Use pagination if more than 5 events
    if len(active_events) > 5:
        view = EventListView(active_events, user_id, tz, guild_id)
        await interaction.followup.send(embed=view.get_embed(), view=view)
        return
    
    # Simple embed for <=5 events
    embed = discord.Embed(
        title='📅 Daftar Event',
        description=f'Total: **{len(active_events)}** event aktif',
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url='https://i.pinimg.com/736x/9d/9f/e0/9d9fe08a397670cf7aa24facaddcceee.jpg')
    
    for idx, e in enumerate(active_events, start=1):
        # Handle both old 'tanggal' and new 'tanggal_mulai' field names
        event_time = e.get('tanggal_mulai') or e.get('tanggal')
        event_utc = ensure_aware_utc(event_time)
        ev_local = event_utc.astimezone(ZoneInfo(tz))
        short_id = str(e['_id'])[:8]
        desc = e.get('deskripsi') or '_Tidak ada deskripsi_'
        
        # Build event time info
        time_info = f'{ev_local.strftime("%H:%M")}'
        # Add end time if available
        if 'tanggal_selesai' in e and e['tanggal_selesai']:
            end_utc = ensure_aware_utc(e['tanggal_selesai'])
            end_local = end_utc.astimezone(ZoneInfo(tz))
            time_info += f' - {end_local.strftime("%H:%M")}'
        
        field_name = f'{idx}. {e["judul"]}'
        field_value = (
            f'{desc}\n'
            f'🆔 `{short_id}`'
        )
        embed.add_field(name=field_name, value=field_value, inline=False)
        
        # Add time as separate inline field
        date_str = format_date(ev_local)
        embed.add_field(name='📅 Tanggal', value=date_str, inline=True)
        embed.add_field(name='⏰ Waktu', value=time_info, inline=True)

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
            changes.append(f"**Deadline:** `{format_date(old_dl)} {old_dl.strftime('%H:%M')}` → `{format_date(new_dl)} {new_dl.strftime('%H:%M')}`")
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
    
    embed.add_field(name='📅 Deadline Sekarang', value=f'{format_date(final_deadline)} ({final_deadline.strftime("%H:%M %Z")})', inline=False)
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
        value=f'{format_date(dl_local)} ({dl_local.strftime("%H:%M %Z")})\n{time_status}',
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


@tree.command(name='doneevent', description='Tandai event selesai dengan ID atau nomor')
@app_commands.describe(identifier='ObjectId (prefix 8 chars ok) atau nomor urut dari /list')
async def doneevent(interaction: discord.Interaction, identifier: str):
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else None

    # Try as number (index) - need to count position in combined list
    target = None
    if identifier.isdigit():
        idx = int(identifier) - 1
        
        # Fetch tasks and events to find the position
        task_cursor = events_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False}).sort('tanggal_mulai', 1)
        event_rows = list(task_cursor)
        
        now_utc = datetime.now(ZoneInfo('UTC'))
        active_rows = [e for e in event_rows if ensure_aware_utc(e['tanggal_mulai']) > now_utc]
        
        if 0 <= idx < len(active_rows):
            target = active_rows[idx]
    else:
        # try match by prefix or full ObjectId
        try:
            candidate = events_col.find_one({'_id': ObjectId(identifier)})
            if candidate and candidate['user_id'] == user_id and candidate.get('guild_id') == guild_id:
                target = candidate
        except Exception:
            cursor = events_col.find({'user_id': user_id, 'guild_id': guild_id, 'status': False})
            for r in cursor:
                if str(r['_id']).startswith(identifier):
                    target = r
                    break

    if not target:
        embed = discord.Embed(
            title='❌ Event Tidak Ditemukan',
            description=f'Tidak dapat menemukan event dengan identifier: `{identifier}`\n\n💡 **Tips:**\n• Gunakan `/list` untuk melihat nomor atau ID event\n• ID harus minimal 8 karakter pertama\n• Pastikan event milik kamu dan belum selesai',
            color=discord.Color.red()
        )
        embed.set_thumbnail(url='https://i.pinimg.com/736x/9c/6d/96/9c6d96bb7d1e10e4ce0de2914329be36.jpg')
        embed.set_footer(text=f'Diminta oleh {interaction.user.display_name}', icon_url=interaction.user.display_avatar.url)
        embed.timestamp = datetime.now(ZoneInfo('UTC'))
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Mark as completed
    now_utc = datetime.now(ZoneInfo('UTC'))
    events_col.update_one({'_id': target['_id']}, {'$set': {'status': True, 'completed_at': now_utc}})
    
    # Get user timezone for display
    tz = await get_user_timezone(user_id)
    event_utc = ensure_aware_utc(target['tanggal_mulai'])
    ev_local = event_utc.astimezone(ZoneInfo(tz))
    created_utc = ensure_aware_utc(target.get('created_at', now_utc))
    
    # Calculate if event was completed on time or late
    time_diff = (now_utc - event_utc).total_seconds()
    if time_diff < 0:
        status_emoji = '🎉'
        status_text = '**Event Sudah diselesaikan!**'
        status_color = discord.Color.dark_green()
        time_status = f'Diselesaikan **{human_delta(timedelta(seconds=abs(time_diff)))}** sebelum waktu'
    else:
        status_emoji = '⏰'
        status_text = '**Event Selesai Terlambat**'
        status_color = discord.Color.orange()
        time_status = f'Diselesaikan **{human_delta(timedelta(seconds=time_diff))}** setelah waktu'
    
    # Create completion embed
    embed = discord.Embed(
        title=f'{status_emoji} Event Diselesaikan!',
        description=f'{status_text}\n\n**{target["judul"]}**\n_{target.get("deskripsi") or "Tidak ada deskripsi"}_',
        color=status_color
    )
    
    embed.set_thumbnail(url='https://i.pinimg.com/736x/6e/7c/2f/6e7c2f925eff6444fb4d8992f1330768.jpg')
    
    embed.add_field(
        name='📅 Tanggal Event',
        value=f'{format_date(ev_local)} ({ev_local.strftime("%H:%M %Z")})\n{time_status}',
        inline=False
    )
    
    embed.add_field(
        name='⏱️ Durasi',
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
    
    embed.set_footer(text=f'ID: {str(target["_id"])[:8]}', icon_url=interaction.user.display_avatar.url)
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
        color=discord.Color.blurple()
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
            f'📅 **Deadline:** {format_date(dl_local)} ({dl_local.strftime("%H:%M")})\n'
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
    embed.add_field(name='🔔 Reminder Waktu', value=f'{format_date(reminder_local)} ({reminder_local.strftime("%H:%M %Z")})', inline=False)
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
    embed.add_field(name='📅 Deadline', value=f'{format_date(dl_local)} ({dl_local.strftime("%H:%M %Z")})', inline=False)
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
    if gs and gs.get('channel_id_task'):
        channel = bot.get_channel(int(gs['channel_id_task']))

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


async def send_reminder_for_event(edoc, threshold_hours: int):
    user_id = edoc['user_id']
    guild_id = edoc.get('guild_id')
    title = edoc.get('judul')
    # Handle both old 'tanggal' and new 'tanggal_mulai' field names for backward compatibility
    event_time = edoc.get('tanggal_mulai') or edoc.get('tanggal')
    if not event_time:
        LOG.error('Event document missing both tanggal_mulai and tanggal: %s', edoc.get('_id'))
        return
    event_utc = ensure_aware_utc(event_time)
    # fetch user's timezone from DB in executor to avoid blocking
    loop = asyncio.get_running_loop()
    user_doc = await loop.run_in_executor(None, lambda: users_col.find_one({'user_id': user_id}))
    tz = (user_doc or {}).get('timezone', DEFAULT_TZ)
    ev_local = event_utc.astimezone(ZoneInfo(tz))
    delta = event_utc - datetime.now(ZoneInfo('UTC'))
    countdown = human_delta(delta)

    # Emoji and color based on urgency
    if threshold_hours == 0:
        emoji = '🚨'
        color = discord.Color.dark_red()
        urgency = 'EVENT DIMULAI!'
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
        description=f'**{title}**\n\n{edoc.get("deskripsi") or "_Tidak ada deskripsi_"}',
        color=color
    )
    embed.add_field(name='📅 Tanggal Event', value=f'{format_date(ev_local)} ({ev_local.strftime("%H:%M %Z")})', inline=False)
    embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
    embed.add_field(name='👤 Pembuat', value=f'<@{user_id}>', inline=True)
    embed.set_footer(text=f'ID: {str(edoc["_id"])[:8]} • Gunakan /doneevent untuk menyelesaikan')
    embed.timestamp = datetime.now(ZoneInfo('UTC'))

    if not guild_id:
        LOG.warning('Event %s has no guild_id, skipping reminder (no guild target).', edoc['_id'])
        return

    # Prefer configured channel in guild_settings
    gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
    channel = None
    if gs and gs.get('channel_id_event'):
        channel = bot.get_channel(int(gs['channel_id_event']))

    # fallback: guild.system_channel or first accessible text channel
    if channel is None:
        g = bot.get_guild(guild_id)
        if not g:
            LOG.debug('Guild %s not in cache; skipping reminder for event %s', guild_id, edoc['_id'])
            return
        channel = g.system_channel
        if channel is None:
            for c in g.channels:
                if isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages:
                    channel = c
                    break

    if channel is None:
        LOG.warning('No available channel to send reminder for guild %s (event %s)', guild_id, edoc['_id'])
        return

    try:
        await channel.send(content=f'<@{user_id}>', embed=embed)
    except Exception:
        LOG.exception('Failed to send reminder in channel %s for event %s', getattr(channel, 'id', None), edoc['_id'])


def mark_reminder_sent(task_id, label):
    # synchronous helper still available
    tasks_col.update_one({'_id': task_id}, {'$addToSet': {'reminders_sent': label}})


async def mark_reminder_sent_async(task_id, label, collection=None):
    if collection is None:
        collection = tasks_col
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: collection.update_one({'_id': task_id}, {'$addToSet': {'reminders_sent': label}}))


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
                        embed.add_field(name='📅 Deadline', value=f'{format_date(dl_local)} ({dl_local.strftime("%H:%M %Z")})', inline=False)
                        embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
                        embed.add_field(name='🔔 Reminder Diatur', value=f'{format_date(reminder_local)} ({reminder_local.strftime("%H:%M")})', inline=True)
                        embed.set_footer(text=f'ID: {str(tdoc["_id"])[:8]} • Gunakan /done untuk menyelesaikan')
                        embed.timestamp = now_utc
                        
                        # Send to configured channel
                        gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
                        channel = None
                        if gs and gs.get('channel_id_task'):
                            channel = bot.get_channel(int(gs['channel_id_task']))
                        
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
    
    # Check events as well
    try:
        event_rows = await loop.run_in_executor(None, lambda: list(events_col.find({'status': False})))
    except ServerSelectionTimeoutError as e:
        LOG.exception('MongoDB ServerSelectionTimeoutError in check_reminders for events: %s', e)
        event_rows = []
    
    for edoc in event_rows:
        # DB stores event dates in UTC; normalize to aware UTC and then convert to DEFAULT_TZ
        # Handle both old 'tanggal' and new 'tanggal_mulai' field names for backward compatibility
        event_time = edoc.get('tanggal_mulai') or edoc.get('tanggal')
        if not event_time:
            continue
        event_utc = ensure_aware_utc(event_time)
        event_date = event_utc.astimezone(ZoneInfo(DEFAULT_TZ))
        seconds_left = (event_date - now).total_seconds()

        # Pre-event reminders (72h, 24h, 5h)
        for th in REMINDER_THRESHOLDS:
            label = f'rem_{th}h'
            if label in edoc.get('reminders_sent', []):
                continue
            target_seconds = th * 3600
            if target_seconds - 90 <= seconds_left <= target_seconds + 90:
                try:
                    await send_reminder_for_event(edoc, th)
                    await mark_reminder_sent_async(edoc['_id'], label, events_col)
                    LOG.info('Sent %sh reminder for event %s', th, edoc['_id'])
                except Exception:
                    LOG.exception('Failed to send reminder for event %s', edoc['_id'])

        # Due notification: send once when event time has passed or is now
        if seconds_left <= 0 and 'rem_due' not in edoc.get('reminders_sent', []):
            try:
                await send_reminder_for_event(edoc, 0)
                await mark_reminder_sent_async(edoc['_id'], 'rem_due', events_col)
                LOG.info('Sent due reminder for event %s', edoc['_id'])
            except Exception:
                LOG.exception('Failed to send due reminder for event %s', edoc['_id'])
        
        # Auto-delete events that are overdue by more than 24 hours
        if seconds_left < -86400:
            try:
                await loop.run_in_executor(None, lambda: events_col.delete_one({'_id': edoc['_id']}))
                LOG.info('Auto-deleted overdue event %s (overdue by %.1f hours)', edoc['_id'], abs(seconds_left / 3600))
            except Exception:
                LOG.exception('Failed to auto-delete overdue event %s', edoc['_id'])
        
        # Custom reminders for events
        custom_reminders = edoc.get('custom_reminders', [])
        if custom_reminders:
            now_utc = datetime.now(ZoneInfo('UTC'))
            for idx, reminder in enumerate(custom_reminders):
                if reminder.get('sent', False):
                    continue
                reminder_time = ensure_aware_utc(reminder['time'])
                diff = (now_utc - reminder_time).total_seconds()
                if -90 <= diff <= 90:
                    try:
                        user_id = edoc['user_id']
                        guild_id = edoc.get('guild_id')
                        title = edoc.get('judul')
                        
                        user_doc = await loop.run_in_executor(None, lambda: users_col.find_one({'user_id': user_id}))
                        tz = (user_doc or {}).get('timezone', DEFAULT_TZ)
                        reminder_local = reminder_time.astimezone(ZoneInfo(tz))
                        ev_local = event_utc.astimezone(ZoneInfo(tz))
                        delta = event_utc - now_utc
                        countdown = human_delta(delta)
                        
                        embed = discord.Embed(
                            title='🔔 Custom Reminder!',
                            description=f'**{title}**\n\n{edoc.get("deskripsi") or "_Tidak ada deskripsi_"}',
                            color=discord.Color.blue()
                        )
                        embed.add_field(name='📅 Tanggal Event', value=f'{format_date(ev_local)} ({ev_local.strftime("%H:%M %Z")})', inline=False)
                        embed.add_field(name='⏰ Waktu Tersisa', value=countdown, inline=True)
                        embed.add_field(name='🔔 Reminder Diatur', value=f'{format_date(reminder_local)} ({reminder_local.strftime("%H:%M")})', inline=True)
                        embed.set_footer(text=f'ID: {str(edoc["_id"])[:8]} • Gunakan /doneevent untuk menyelesaikan')
                        embed.timestamp = now_utc
                        
                        gs = await loop.run_in_executor(None, lambda: guilds_col.find_one({'guild_id': guild_id}))
                        channel = None
                        if gs and gs.get('channel_id_event'):
                            channel = bot.get_channel(int(gs['channel_id_event']))
                        
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
                        
                        custom_reminders[idx]['sent'] = True
                        await loop.run_in_executor(None, lambda: events_col.update_one(
                            {'_id': edoc['_id']},
                            {'$set': {'custom_reminders': custom_reminders}}
                        ))
                        LOG.info('Sent custom reminder for event %s at %s', edoc['_id'], reminder_time)
                    except Exception:
                        LOG.exception('Failed to send custom reminder for event %s', edoc['_id'])


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
            if gs and gs.get('channel_id_task'):
                channel = bot.get_channel(int(gs['channel_id_task']))
            
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
                description=f'**Tugas yang akan dikerjakan pada minggu {format_date(last_monday).split()[1]} - {format_date(last_saturday)}**\n\n'
                           f'Daftar tugas dengan deadline selama minggu ini.',
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
