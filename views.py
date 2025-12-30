"""Discord UI components - Views, Buttons, and Modals."""

from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from utils import format_date, human_delta, ensure_aware_utc
from database import tasks_col, events_col


class TaskListView(discord.ui.View):
    """Pagination view for task lists."""
    
    def __init__(self, tasks, user_id, tz, guild_id):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.tasks = tasks
        self.user_id = user_id
        self.tz = tz
        self.guild_id = guild_id
        self.current_page = 0
        self.items_per_page = 5
        self.total_pages = (len(tasks) - 1) // self.items_per_page + 1 if tasks else 1
        self.update_buttons()
    
    def update_buttons(self):
        """Update button enabled/disabled states based on current page."""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
    
    def get_embed(self):
        """Generate embed for current page."""
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
                f'📅 **Deadline:** {format_date(dl_local)} {dl_local.strftime("%H:%M")}\n'
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


class EventListView(discord.ui.View):
    """Pagination view for event lists."""
    
    def __init__(self, events, user_id, tz, guild_id):
        super().__init__(timeout=300)
        self.events = events
        self.user_id = user_id
        self.tz = tz
        self.guild_id = guild_id
        self.current_page = 0
        self.items_per_page = 5
        self.total_pages = (len(events) - 1) // self.items_per_page + 1 if events else 1
        self.update_buttons()
    
    def update_buttons(self):
        """Update button enabled/disabled states based on current page."""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
    
    def get_embed(self):
        """Generate embed for current page."""
        now_utc = datetime.now(ZoneInfo('UTC'))
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(self.events))
        page_events = self.events[start_idx:end_idx]
        
        embed = discord.Embed(
            title='📅 Daftar Event Aktif',
            description=f'Total: **{len(self.events)}** event aktif',
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url='https://i.pinimg.com/736x/87/8b/74/878b7473dc8d68673b9ae5f3ca4103ba.jpg')
        
        for idx, e in enumerate(page_events, start=start_idx + 1):
            event_utc = ensure_aware_utc(e['tanggal_mulai'])
            ev_local = event_utc.astimezone(ZoneInfo(self.tz))
            short_id = str(e['_id'])[:8]
            desc = e.get('deskripsi') or '_Tidak ada deskripsi_'
            
            # Build event time info
            time_info = f'{ev_local.strftime("%H:%M")}'
            # Add end time if available
            if 'tanggal_selesai' in e and e['tanggal_selesai']:
                end_utc = ensure_aware_utc(e['tanggal_selesai'])
                end_local = end_utc.astimezone(ZoneInfo(self.tz))
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
        
        embed.set_footer(text=f'Halaman {self.current_page + 1}/{self.total_pages}')
        embed.timestamp = now_utc
        return embed
    
    @discord.ui.button(label='◀️ Prev', style=discord.ButtonStyle.gray, custom_id='prev_event')
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list event kamu!', ephemeral=True)
            return
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    
    @discord.ui.button(label='Next ▶️', style=discord.ButtonStyle.gray, custom_id='next_event')
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list event kamu!', ephemeral=True)
            return
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    
    @discord.ui.button(label='🔄 Refresh', style=discord.ButtonStyle.green, custom_id='refresh_event')
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ Ini bukan list event kamu!', ephemeral=True)
            return
        # Re-fetch events
        cursor = events_col.find({'user_id': self.user_id, 'guild_id': self.guild_id, 'status': False}).sort('tanggal_mulai', 1)
        rows = list(cursor)
        now_utc = datetime.now(ZoneInfo('UTC'))
        self.events = [e for e in rows if ensure_aware_utc(e['tanggal_mulai']) > now_utc]
        self.total_pages = (len(self.events) - 1) // self.items_per_page + 1 if self.events else 1
        self.current_page = min(self.current_page, self.total_pages - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
