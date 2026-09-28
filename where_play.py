"""One persistent, periodically refreshed WardogServers card per Discord guild."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger('clan_bot.where_play')
MAX_CALLERS = 5
CALLER_ACTIVITIES = ('Штурм', 'Стройка', 'ДРГ', 'Разведка')


def caller_slot_available(db, guild, member_id):
    owners = {
        owner for owner, channel_id in db.execute(
            'SELECT member, channel FROM caller_rooms WHERE guild=?', (guild.id,)
        ).fetchall()
        if isinstance(guild.get_channel(channel_id), discord.VoiceChannel)
    }
    return member_id in owners or len(owners) < MAX_CALLERS


async def confirm(interaction, text):
    message = await interaction.followup.send(text, ephemeral=True, wait=True)
    await message.delete(delay=2)


def join_code(value):
    value = value.strip()
    if re.fullmatch(r'[0-9]{1,32}', value):
        return value  # Preserve leading zeros.
    if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
        return value.lower()
    raise ValueError('Нужен код подключения из игры: число или UUID community-сервера.')


def find_server(snapshot, code):
    matches = [s for s in snapshot['data'] if str(s.get('serverId', '')).lower() == code.lower()]
    if len(matches) > 1:
        raise ValueError('API вернул несколько серверов с этим кодом. Повторите позже.')
    return matches[0] if matches else None


def stale(snapshot):
    meta = snapshot['meta']
    stamp = datetime.fromisoformat(meta['fetchedAt'].replace('Z', '+00:00'))
    return bool(meta.get('stale', True)) or (datetime.now(timezone.utc) - stamp).total_seconds() > float(meta['refreshSeconds']) + 60


class ServerAPI:
    def __init__(self):
        self.session = None
        self.snapshot = None
        self.etag = None
        self.next_request = 0
        self.error = None
        self.last_attempt = None
        self.last_success = None
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if time.monotonic() < self.next_request:
                if self.error:
                    raise ValueError(self.error)
                return self.snapshot
            started = time.monotonic()
            self.next_request = started + 60
            self.last_attempt = datetime.now(timezone.utc)
            try:
                if self.session is None:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
                headers = {'If-None-Match': self.etag} if self.etag and self.snapshot and not stale(self.snapshot) else {}
                async with self.session.get('https://api.wardogservers.com/v1/snapshot', headers=headers) as response:
                    if response.status == 429:
                        delay = response.headers.get('Retry-After', '60')
                        self.next_request = time.monotonic() + max(60, float(delay) if delay.isdigit() else 60)
                    if response.status == 304 and self.snapshot is not None:
                        payload = self.snapshot
                    else:
                        response.raise_for_status()
                        payload = await response.json()
                        if not isinstance(payload.get('data'), list) or not isinstance(payload.get('meta'), dict):
                            raise ValueError('Неподдерживаемый формат API')
                        stale(payload)  # Validate timestamp and refresh interval before caching.
                        self.etag = response.headers.get('ETag')
                        self.snapshot = payload
                    self.next_request = started + max(60, float(payload['meta']['refreshSeconds']))
                    self.error = None
                    self.last_success = datetime.now(timezone.utc)
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as error:
                detail = f'HTTP {error.status}' if isinstance(error, aiohttp.ClientResponseError) else type(error).__name__
                log.warning('WardogServers unavailable: %s', detail)
                self.error = f'WardogServers недоступен ({detail}). Автоматический повтор запланирован; текущий выбор сохранён.'
                raise ValueError(self.error) from error

    async def close(self):
        if self.session is not None:
            await self.session.close()


def card(row, warning=None):
    # guild, channel, role, message, code, team, caller, active, last_json
    embed = discord.Embed(title='🎮 BEDA — где играем', color=discord.Color.blue())
    embed.set_footer(text='Данные: WardogServers • Цвет команды задаёт коллер')
    if not row[7]:
        embed.description = 'Сейчас общего сбора нет.'
    else:
        data = json.loads(row[8]) if row[8] else {}
        server = data.get('server', {})
        embed.description = warning or 'Скопируйте код подключения, найдите сервер в игре и выберите нашу команду.'
        if warning:
            embed.color = discord.Color.orange()
        embed.add_field(name='Сервер', value=discord.utils.escape_markdown(server.get('name') or 'Неизвестно')[:1024], inline=False)
        embed.add_field(name='Код подключения', value=f'`{row[4]}`')
        embed.add_field(name='Регион', value=server.get('region') or '—')
        embed.add_field(name='Карта', value=(server.get('map') or {}).get('variant') or '—')
        label = 'Игроки (последние данные)' if warning else 'Игроки'
        embed.add_field(name=label, value=f"{server.get('players', '—')} / {server.get('maxPlayers', '—')}")
        embed.add_field(name='Наша команда', value=row[5])
        embed.add_field(name='Сбор объявил', value=f'<@{row[6]}>')
        if data.get('fetchedAt'):
            stamp = datetime.fromisoformat(data['fetchedAt'].replace('Z', '+00:00'))
            embed.add_field(name='Данные обновлены', value=f'<t:{int(stamp.timestamp())}:f> · <t:{int(stamp.timestamp())}:R>', inline=False)
    embed.add_field(name='Источник', value='[WardogServers](https://wardogservers.com) — независимый сервис', inline=False)
    return embed


class CallerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction, error, item):
        log.error('Ошибка комнаты коллера', exc_info=(type(error), error, error.__traceback__))
        text = 'Не удалось создать комнату или перенести участника. Проверьте права бота на управление каналами и перемещение участников.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label='Я коллер', emoji='🎖️', style=discord.ButtonStyle.success, custom_id='beda:caller:join:v1')
    async def join(self, interaction, button):
        await join_caller(interaction, interaction.message)


class CallerActivityView(discord.ui.View):
    def __init__(self, owner, card_message):
        super().__init__(timeout=180)
        self.owner = owner
        self.card_message = card_message

    on_error = CallerView.on_error

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Этот выбор открыт для другого участника.', ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder='Вид деятельности',
                       options=[discord.SelectOption(label=value) for value in CALLER_ACTIVITIES])
    async def choose(self, interaction, select):
        await join_caller(interaction, self.card_message, select.values[0])


async def join_caller(interaction, card_message, activity=None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if activity is not None and activity not in CALLER_ACTIVITIES:
        await interaction.followup.send('Выберите деятельность из списка.', ephemeral=True)
        return
    bot = interaction.client
    feature = bot.where_play
    async with feature.lock:
        row = feature.row(interaction.guild_id)
        if not row or not row[7] or row[3] != card_message.id:
            await interaction.followup.send('Общий сбор не активен или это старая карточка.', ephemeral=True)
            return
        if not feature.authorized(interaction, row):
            await interaction.followup.send('Кнопка доступна только роли коллеров из /setup_where_play и администраторам.', ephemeral=True)
            return
        member = interaction.user
        if not member.voice or not member.voice.channel:
            message = await interaction.followup.send('Сначала зайдите в любой голосовой канал, затем нажмите «Я коллер» — бот перенесёт вас в комнату.', ephemeral=True, wait=True)
            await message.delete(delay=10)
            return
        config = bot.db.execute('SELECT category FROM voice_hubs WHERE guild=?', (interaction.guild_id,)).fetchone()
        category = interaction.guild.get_channel(config[0]) if config else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send('Администратор должен настроить категорию комнат через /setup_voice_rooms.', ephemeral=True)
            return
        permissions = category.permissions_for(member)
        if not permissions.view_channel or not permissions.connect:
            await interaction.followup.send('У вас нет доступа к категории голосовых комнат.', ephemeral=True)
            return
        if activity is None:
            await interaction.followup.send('Выберите вид деятельности:',
                view=CallerActivityView(member.id, card_message), ephemeral=True)
            return
        async with bot.temp_voice.lock:
            if not caller_slot_available(bot.db, interaction.guild, member.id):
                await interaction.followup.send('Уже есть 5 комнат коллеров. Новый коллер сможет присоединиться, когда одна из комнат опустеет и будет удалена.', ephemeral=True)
                return
            saved = bot.db.execute('SELECT channel FROM caller_rooms WHERE guild=? AND member=?', (interaction.guild_id, member.id)).fetchone()
            room = interaction.guild.get_channel(saved[0]) if saved else None
            if not isinstance(room, discord.VoiceChannel):
                # Reuse an existing personal room instead of making a duplicate.
                room = None
                for (room_id,) in bot.db.execute('SELECT channel FROM temporary_voice WHERE guild=? AND owner=?', (interaction.guild_id, member.id)).fetchall():
                    candidate = interaction.guild.get_channel(room_id)
                    if isinstance(candidate, discord.VoiceChannel):
                        room = candidate
                        break
            created = room is None
            room_name = f"{activity} - {' '.join(member.display_name.split())}"[:100]
            if created:
                room = await interaction.guild.create_voice_channel(room_name, category=category, reason='Комната коллера BEDA')
                with bot.db:
                    bot.db.execute('INSERT INTO temporary_voice VALUES (?, ?, ?, ?)', (room.id, interaction.guild_id, member.id, time.time()))
            try:
                if not created and room.name != room_name:
                    await room.edit(name=room_name, reason='Выбор деятельности коллера')
                await member.move_to(room, reason='Я коллер — присоединение к комнате')
            except Exception:
                if created and not room.members:
                    try:
                        await room.delete(reason='Не удалось перенести коллера')
                        with bot.db:
                            bot.db.execute('DELETE FROM temporary_voice WHERE channel=?', (room.id,))
                    except discord.HTTPException:
                        log.exception('Комната %s будет удалена при фоновой очистке', room.id)
                raise
            with bot.db:
                bot.db.execute('INSERT OR REPLACE INTO caller_rooms VALUES (?, ?, ?)', (interaction.guild_id, member.id, room.id))
        # Keep the current server warning and timestamps; only rebuild the roster.
        embed = card_message.embeds[0].copy() if card_message.embeds else card(row)
        for index in reversed(range(len(embed.fields))):
            if embed.fields[index].name == '🎖️ Коллеры':
                embed.remove_field(index)
        # Voice cache may catch up just after move_to; refresh also runs every 15s.
        await feature.publish(row, embed)
    await confirm(interaction, f'Ваша комната: {room.mention}. Список коллеров обновится в течение 15 секунд.')

class WherePlay:
    def __init__(self, bot):
        self.bot = bot
        self.api = ServerAPI()
        self.lock = asyncio.Lock()
        bot.db.execute('''CREATE TABLE IF NOT EXISTS where_play (
            guild INTEGER PRIMARY KEY, channel INTEGER, role INTEGER, message INTEGER,
            code TEXT, team TEXT, caller INTEGER, active INTEGER DEFAULT 0, last_json TEXT)''')
        bot.db.commit()
        bot.db.execute('CREATE TABLE IF NOT EXISTS caller_rooms (guild INTEGER, member INTEGER, channel INTEGER, PRIMARY KEY(guild, member))')
        bot.db.commit()
        self.register()

    def row(self, guild):
        return self.bot.db.execute('SELECT guild, channel, role, message, code, team, caller, active, last_json FROM where_play WHERE guild=?', (guild,)).fetchone()

    def authorized(self, interaction, row):
        return interaction.user.guild_permissions.administrator or (row and row[2] in [r.id for r in interaction.user.roles])

    async def publish(self, row, embed):
        if row[7]:
            guild = self.bot.get_guild(row[0])
            lines = []
            for member_id, room_id in self.bot.db.execute('SELECT member, channel FROM caller_rooms WHERE guild=?', (row[0],)).fetchall():
                room = guild.get_channel(room_id) if guild else None
                if isinstance(room, discord.VoiceChannel) and any(m.id == member_id for m in room.members):
                    line = f'<@{member_id}> — <#{room_id}>'
                    if len('\n'.join(lines + [line])) < 950:
                        lines.append(line)
                    else:
                        lines.append('…остальные комнаты доступны в списке голосовых каналов.')
                        break
            embed.add_field(name='🎖️ Коллеры', value='\n'.join(lines) or 'Пока нет. Нажмите «Я коллер», находясь в голосовом канале.', inline=False)
        view = CallerView()
        view.children[0].disabled = not bool(row[7])
        channel = await self.bot.fetch_channel(row[1])
        if row[3]:
            try:
                await channel.get_partial_message(row[3]).edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.NotFound:
                pass
        message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        with self.bot.db:
            self.bot.db.execute('UPDATE where_play SET message=? WHERE guild=?', (message.id, row[0]))

    async def refresh(self):
        async with self.lock:
            rows = self.bot.db.execute('SELECT guild FROM where_play WHERE active=1').fetchall()
            if not rows:
                return
            try:
                snapshot = await self.api.get()
                warning = '⚠️ Источник передаёт устаревшие данные.' if stale(snapshot) else None
            except ValueError as error:
                snapshot, warning = None, f'⚠️ {error}\nНиже последние известные данные.'
            for (guild,) in rows:
                try:
                    row = self.row(guild)
                    current_warning = warning
                    if snapshot is not None:
                        server = find_server(snapshot, row[4])
                        if server:
                            data = json.dumps({'server': server, 'fetchedAt': snapshot['meta']['fetchedAt']}, ensure_ascii=False)
                            previous = json.loads(row[8]) if row[8] else {}
                            old_time = datetime.fromisoformat(previous['fetchedAt'].replace('Z', '+00:00')) if previous.get('fetchedAt') else datetime.min.replace(tzinfo=timezone.utc)
                            new_time = datetime.fromisoformat(snapshot['meta']['fetchedAt'].replace('Z', '+00:00'))
                            if new_time >= old_time:
                                with self.bot.db:
                                    self.bot.db.execute('UPDATE where_play SET last_json=? WHERE guild=?', (data, guild))
                            row = self.row(guild)
                        elif not warning:
                            current_warning = '⚠️ Сервер отсутствует в свежем списке. Ниже последние известные данные; другой сервер не выбран.'
                    embed = card(row, current_warning)
                    if self.api.last_attempt:
                        interval = max(60, float(self.api.snapshot['meta']['refreshSeconds'])) if self.api.snapshot else 60
                        embed.add_field(name='Автообновление', value=f'Обращение к API: <t:{int(self.api.last_attempt.timestamp())}:R>\nИнтервал API: {int(interval)} сек.', inline=False)
                    await self.publish(row, embed)
                except Exception:
                    log.exception('Не удалось обновить карточку сервера %s', guild)

    @tasks.loop(seconds=15)
    async def poll(self):
        await self.refresh()

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def register(self):
        @self.bot.tree.command(name='setup_where_play', description='Настроить канал карточки «Где играем» и роль коллеров')
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        @app_commands.describe(channel='Канал для карточки', caller_role='Роль коллеров; без неё управление только у администраторов')
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel, caller_role: discord.Role | None = None):
            if caller_role and (caller_role.is_default() or caller_role.managed):
                await interaction.response.send_message('Выберите обычную роль коллеров.', ephemeral=True)
                return
            permissions = channel.permissions_for(interaction.guild.me)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                await interaction.response.send_message('Боту нужны права просмотра канала, отправки сообщений и встраивания ссылок.', ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.lock:
                old = self.row(interaction.guild_id)
                if old and old[1] != channel.id:
                    if old[3]:
                        try:
                            previous = await self.bot.fetch_channel(old[1])
                            await previous.get_partial_message(old[3]).delete()
                        except discord.NotFound:
                            pass
                    with self.bot.db:
                        self.bot.db.execute('UPDATE where_play SET channel=?, message=NULL WHERE guild=?', (channel.id, interaction.guild_id))
                with self.bot.db:
                    self.bot.db.execute('INSERT INTO where_play(guild, channel, role) VALUES (?, ?, ?) ON CONFLICT(guild) DO UPDATE SET role=excluded.role', (interaction.guild_id, channel.id, caller_role.id if caller_role else None))
                row = self.row(interaction.guild_id)
                await self.publish(row, card(row, 'ℹ️ Ожидается очередное обновление данных.' if row[7] else None))
            await confirm(interaction, f'Карточка настроена в {channel.mention}. Используйте /where_play и /stop_play.')

        @self.bot.tree.command(name='where_play', description='Указать сервер и команду общего сбора BEDA')
        @app_commands.guild_only()
        @app_commands.describe(server='Код подключения из игры (не название сервера)', team='Выберите нашу команду')
        @app_commands.choices(team=[
            app_commands.Choice(name='🔵 Синие', value='🔵 Синие'),
            app_commands.Choice(name='🔴 Красные', value='🔴 Красные'),
            app_commands.Choice(name='🟢 Зелёные', value='🟢 Зелёные'),
        ])
        async def where(interaction: discord.Interaction, server: str, team: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.lock:
                row = self.row(interaction.guild_id)
                if not row:
                    await interaction.followup.send('Администратор должен сначала выполнить /setup_where_play.', ephemeral=True)
                    return
                if not self.authorized(interaction, row):
                    await interaction.followup.send('Управление доступно только администраторам и коллерам.', ephemeral=True)
                    return
                try:
                    code = join_code(server)
                    if team not in ('🔵 Синие', '🔴 Красные', '🟢 Зелёные'):
                        raise ValueError('Выберите команду из списка: 🔵 Синие, 🔴 Красные или 🟢 Зелёные.')
                    snapshot = await self.api.get()
                    if stale(snapshot):
                        raise ValueError('Данные API устарели. Текущая карточка сохранена; повторите позже.')
                    selected = find_server(snapshot, code)
                    if not selected:
                        raise ValueError('Код не найден в актуальном списке. Проверьте код подключения из игры и повторите позже. Текущий сервер не изменён.')
                except ValueError as error:
                    await interaction.followup.send(str(error), ephemeral=True)
                    return
                data = json.dumps({'server': selected, 'fetchedAt': snapshot['meta']['fetchedAt']}, ensure_ascii=False)
                new_row = (*row[:4], code, team.strip(), interaction.user.id, 1, data)
                await self.publish(new_row, card(new_row))
                with self.bot.db:
                    self.bot.db.execute('UPDATE where_play SET code=?, team=?, caller=?, active=1, last_json=? WHERE guild=?', (code, team.strip(), interaction.user.id, data, interaction.guild_id))
            await confirm(interaction, f'Сервер общего сбора обновлён в <#{row[1]}>.')

        @self.bot.tree.command(name='stop_play', description='Завершить общий сбор BEDA')
        @app_commands.guild_only()
        async def stop(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.lock:
                row = self.row(interaction.guild_id)
                if not row or not self.authorized(interaction, row):
                    await interaction.followup.send('Нужна настройка /setup_where_play и права администратора или коллера.', ephemeral=True)
                    return
                stopped = (*row[:7], 0, row[8])
                await self.publish(stopped, card(stopped))
                with self.bot.db:
                    self.bot.db.execute('UPDATE where_play SET active=0 WHERE guild=?', (interaction.guild_id,))
            await confirm(interaction, 'Общий сбор завершён.')
