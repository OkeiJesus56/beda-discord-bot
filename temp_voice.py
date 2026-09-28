"""Join-to-create voice rooms; only explicitly tracked rooms are deleted."""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger('clan_bot.temp_voice')


class TempVoice:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        bot.db.executescript('''
            CREATE TABLE IF NOT EXISTS voice_hubs (
                guild INTEGER PRIMARY KEY, hub INTEGER NOT NULL, category INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS temporary_voice (
                channel INTEGER PRIMARY KEY, guild INTEGER NOT NULL, owner INTEGER NOT NULL, created REAL NOT NULL);
        ''')
        self.register()

    async def remove_empty(self, channel_id):
        row = self.bot.db.execute('SELECT guild, created FROM temporary_voice WHERE channel=?', (channel_id,)).fetchone()
        if not row:
            return
        guild = self.bot.get_guild(row[0])
        if guild is None or guild.unavailable:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                with self.bot.db:
                    self.bot.db.execute('DELETE FROM temporary_voice WHERE channel=?', (channel_id,))
                return
        if not isinstance(channel, discord.VoiceChannel) or channel.members or time.time() - row[1] < 20:
            return
        # A tracked room cannot be repurposed as a hub through setup.
        await channel.delete(reason='Временная голосовая комната пуста')
        with self.bot.db:
            self.bot.db.execute('DELETE FROM temporary_voice WHERE channel=?', (channel_id,))

    async def voice_changed(self, member, before, after):
        if before.channel == after.channel:
            return
        async with self.lock:
            if before.channel:
                try:
                    await self.remove_empty(before.channel.id)
                except discord.HTTPException:
                    log.exception('Не удалось удалить пустую комнату %s', before.channel.id)
            if member.bot or after.channel is None:
                return
            config = self.bot.db.execute('SELECT hub, category FROM voice_hubs WHERE guild=?', (member.guild.id,)).fetchone()
            if not config or after.channel.id != config[0]:
                return
            if not member.voice or not member.voice.channel or member.voice.channel.id != config[0]:
                return
            category = member.guild.get_channel(config[1])
            if not isinstance(category, discord.CategoryChannel):
                log.warning('Категория временных комнат не найдена: %s', config[1])
                return
            # Reuse the member's occupied room when they return to the hub.
            for (channel_id,) in self.bot.db.execute('SELECT channel FROM temporary_voice WHERE guild=? AND owner=?', (member.guild.id, member.id)).fetchall():
                existing = member.guild.get_channel(channel_id)
                if isinstance(existing, discord.VoiceChannel):
                    await member.move_to(existing, reason='Возврат во временную комнату')
                    return
            permissions = category.permissions_for(member)
            if not permissions.view_channel or not permissions.connect:
                log.warning('Участник %s не имеет доступа к категории комнат', member.id)
                return
            name = ('🔊 ' + ' '.join(member.display_name.split()))[:100]
            room = await member.guild.create_voice_channel(name, category=category, reason='Личная голосовая комната')
            try:
                with self.bot.db:
                    self.bot.db.execute('INSERT INTO temporary_voice VALUES (?, ?, ?, ?)', (room.id, member.guild.id, member.id, time.time()))
                if member.voice and member.voice.channel and member.voice.channel.id == config[0]:
                    await member.move_to(room, reason='Вход в личную голосовую комнату')
            except Exception:
                # Keep the record if cleanup fails; periodic cleanup retries safely.
                if not room.members:
                    try:
                        await room.delete(reason='Не удалось создать/заселить временную комнату')
                        with self.bot.db:
                            self.bot.db.execute('DELETE FROM temporary_voice WHERE channel=?', (room.id,))
                    except discord.HTTPException:
                        log.exception('Не удалось убрать комнату %s', room.id)
                raise

    @tasks.loop(seconds=30)
    async def cleanup(self):
        if not self.bot.is_ready():
            return
        async with self.lock:
            for (channel_id,) in self.bot.db.execute('SELECT channel FROM temporary_voice').fetchall():
                try:
                    await self.remove_empty(channel_id)
                except Exception:
                    log.exception('Ошибка очистки голосовой комнаты %s', channel_id)

    @cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    def register(self):
        @self.bot.tree.command(name='setup_voice_rooms', description='Настроить создание временных голосовых комнат при входе')
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        @app_commands.describe(hub='Голосовой канал «Создать комнату»', category='Категория для временных комнат')
        async def setup(interaction: discord.Interaction, hub: discord.VoiceChannel, category: discord.CategoryChannel):
            if self.bot.db.execute('SELECT 1 FROM temporary_voice WHERE channel=?', (hub.id,)).fetchone():
                await interaction.response.send_message('Выберите постоянный канал, не временную комнату.', ephemeral=True)
                return
            for location in (hub, category):
                permissions = location.permissions_for(interaction.guild.me)
                required = ('view_channel', 'connect', 'move_members') if location == hub else ('view_channel', 'connect', 'move_members', 'manage_channels')
                if not all(getattr(permissions, permission) for permission in required):
                    await interaction.response.send_message('Боту нужны права просмотра, подключения, перемещения участников и управления каналами в выбранной категории; в канале создания — просмотр, подключение и перемещение участников.', ephemeral=True)
                    return
            with self.bot.db:
                self.bot.db.execute('INSERT OR REPLACE INTO voice_hubs VALUES (?, ?, ?)', (interaction.guild_id, hub.id, category.id))
            await interaction.response.send_message(f'Готово. Вход в {hub.mention} создаёт комнату в категории «{category.name}». Комнаты наследуют права категории и удаляются, когда пустеют.', ephemeral=True, delete_after=5)
