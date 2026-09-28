import asyncio
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv
from transcripts import export_ticket
from events import EventModal, EventView, init_events
from where_play import WherePlay, CallerView
from temp_voice import TempVoice
from dossiers import init_dossiers, init_ticket_applications, AdmissionView, approve, register_commands

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
log = logging.getLogger('clan_bot')


def ticket_channel_names(member):
    display = ' '.join(member.display_name.split()) or member.name
    slug = re.sub(r'[^\w-]+', '-', display.lower()).strip('-_')
    if not slug:
        slug = re.sub(r'[^\w-]+', '-', member.name.lower()).strip('-_') or 'player'
    return f'vstyplenie-{slug}'[:100], f'Собеседование-{display}'[:100]


def migrate_tickets(db):
    init_ticket_applications(db)
    if 'voice_channel' not in {row[1] for row in db.execute('PRAGMA table_info(tickets)')}:
        with db:
            db.execute('ALTER TABLE tickets ADD COLUMN voice_channel INTEGER')
    for name, kind in [('archive_channel', 'INTEGER'), ('archive_message', 'INTEGER')]:
        if name not in {row[1] for row in db.execute('PRAGMA table_info(tickets)')}:
            with db:
                db.execute(f'ALTER TABLE tickets ADD COLUMN {name} {kind}')
    db.execute('CREATE TABLE IF NOT EXISTS archives (guild INTEGER PRIMARY KEY, channel INTEGER)')
    db.commit()


async def delete_ticket_voice(guild, channel_id):
    if channel_id is None:
        return
    try:
        channel = await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError('Сохранённый канал собеседования не является голосовым.')
        await channel.delete(reason='Закрытие тикета')
    except discord.NotFound:
        pass


def load_form():
    form = json.loads((ROOT / 'application.json').read_text(encoding='utf-8'))
    if not 1 <= len(form['fields']) <= 5:
        raise ValueError('Анкета должна содержать от 1 до 5 полей.')
    if not 1 <= len(form['title']) <= 45:
        raise ValueError('Название анкеты: от 1 до 45 символов.')
    if len(form['description']) > 4096:
        raise ValueError('Описание слишком длинное.')
    for field in form['fields']:
        if not 1 <= len(field['label']) <= 45 or not 1 <= field.get('max_length', 1000) <= 1000:
            raise ValueError('Название поля: 1–45 символов; длина ответа: 1–1000.')
    return form


class ClanBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, message_content=True, voice_states=True), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.form = load_form()
        self.lock = asyncio.Lock()
        self.db = sqlite3.connect(ROOT / 'tickets.sqlite3')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (guild INTEGER PRIMARY KEY, category INTEGER, role INTEGER);
            CREATE TABLE IF NOT EXISTS tickets (channel INTEGER PRIMARY KEY, guild INTEGER, owner INTEGER, role INTEGER, status TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket ON tickets(guild, owner) WHERE status='open';
        ''')
        migrate_tickets(self.db)
        init_events(self.db)
        init_dossiers(self.db)
        register_commands(self)
        self.where_play = WherePlay(self)
        self.temp_voice = TempVoice(self)

    async def setup_hook(self):
        self.add_view(ApplyView())
        self.add_view(CloseView())
        self.add_view(ArchiveView())
        self.add_view(EventView())
        self.add_view(CallerView())
        self.add_view(AdmissionView())
        guild = discord.Object(id=int(os.environ['DISCORD_GUILD_ID']))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.where_play.poll.start()
        self.temp_voice.cleanup.start()

    async def close(self):
        self.temp_voice.cleanup.cancel()
        cleanup_task = self.temp_voice.cleanup.get_task()
        if cleanup_task:
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        self.where_play.poll.cancel()
        task = self.where_play.poll.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.where_play.api.close()
        await super().close()

    async def on_ready(self):
        log.info('Бот %s подключён.', self.user)

    async def on_voice_state_update(self, member, before, after):
        await self.temp_voice.voice_changed(member, before, after)


async def report_error(interaction, error):
    log.error('Ошибка Discord', exc_info=(type(error), error, error.__traceback__))
    cause = getattr(error, 'original', error)
    text = 'Не удалось выполнить действие. Проверьте права бота и повторите попытку. Подробности в консоли.'
    if isinstance(cause, discord.Forbidden):
        text = f'Discord запретил действие (код {cause.code}). Проверьте права роли BEDA в этом канале: «Просматривать канал» и «Отправлять сообщения». Для ветки также нужно «Отправлять сообщения в ветках». Подробности в консоли.'
    elif isinstance(cause, discord.HTTPException):
        text = f'Ошибка Discord: HTTP {cause.status}, код {cause.code}. Подробности в консоли бота.'
    elif isinstance(cause, FileNotFoundError):
        text = 'Не найден нужный файл бота. Проверьте файлы документов и assets/specializations. Подробности в консоли.'
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def temporary_confirmation(interaction, text):
    message = await interaction.followup.send(text, ephemeral=True, wait=True)
    await message.delete(delay=5)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)


class ApplicationModal(discord.ui.Modal):
    def __init__(self, form):
        super().__init__(title=form['title'])
        for field in form['fields']:
            self.add_item(discord.ui.TextInput(
                label=field['label'], required=field.get('required', True),
                custom_id=field.get('id', field['label']),
                placeholder=field.get('placeholder'),
                max_length=field.get('max_length', 1000),
                style=discord.TextStyle.paragraph if field.get('paragraph') else discord.TextStyle.short,
            ))

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        guild = interaction.guild
        async with bot.lock:
            existing = bot.db.execute("SELECT channel, voice_channel FROM tickets WHERE guild=? AND owner=? AND status='open'", (guild.id, interaction.user.id)).fetchone()
            if existing:
                try:
                    await guild.fetch_channel(existing[0])
                except discord.NotFound:
                    await delete_ticket_voice(guild, existing[1])
                    with bot.db:
                        bot.db.execute("UPDATE tickets SET status='closed' WHERE channel=?", (existing[0],))
                else:
                    await interaction.followup.send(f'У вас уже есть открытый тикет: <#{existing[0]}>', ephemeral=True)
                    return
            settings = bot.db.execute('SELECT category, role FROM settings WHERE guild=?', (guild.id,)).fetchone()
            category = guild.get_channel(settings[0]) if settings else None
            role = guild.get_role(settings[1]) if settings else None
            if not isinstance(category, discord.CategoryChannel) or role is None or role.is_default():
                await interaction.followup.send('Администратор должен настроить тикеты командой /setup_tickets.', ephemeral=True)
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, embed_links=True),
            }
            text_name, voice_name = ticket_channel_names(interaction.user)
            channel = await guild.create_text_channel(text_name, category=category, overwrites=overwrites, reason='Новая заявка в клан')
            voice = None
            try:
                voice_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                    interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
                    role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
                }
                voice = await guild.create_voice_channel(voice_name, category=category, overwrites=voice_overwrites, reason='Собеседование по заявке в клан')
                embed = discord.Embed(title=self.title, description=f'Заявитель: {interaction.user.mention}\nГолосовой канал: {voice.mention}', color=discord.Color.blue())
                for item in self.children:
                    embed.add_field(name=item.label, value=item.value or 'Не указано', inline=False)
                await channel.send(embed=embed, view=CloseView())
                name = next((item.value.strip() for item in self.children if item.custom_id == 'applicant_name'), '')
                greeting = f'Здравствуйте, {discord.utils.escape_markdown(name)}' if name else 'Здравствуйте'
                await channel.send(f'{greeting}, в ближайшее время офицер клана с вами свяжется!', allowed_mentions=discord.AllowedMentions.none())
                with bot.db:
                    bot.db.execute("INSERT INTO tickets (channel, guild, owner, role, status, voice_channel) VALUES (?, ?, ?, ?, 'open', ?)", (channel.id, guild.id, interaction.user.id, role.id, voice.id))
                    answers = {item.custom_id: item.value.strip() for item in self.children}
                    bot.db.execute('INSERT INTO ticket_applications VALUES (?, ?)',
                                   (channel.id, json.dumps(answers, ensure_ascii=False)))
            except Exception:
                for created in (voice, channel):
                    if created is not None:
                        try:
                            await created.delete(reason='Откат незавершённого создания тикета')
                        except discord.HTTPException:
                            log.exception('Не удалось удалить незавершённый канал %s', created.id)
                raise
            await temporary_confirmation(interaction, f'Заявка отправлена! Ваш тикет: {channel.mention}')


class ApplyView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Подать заявку', style=discord.ButtonStyle.success, custom_id='clan:apply:v1')
    async def apply(self, interaction, button):
        if interaction.guild is None:
            await interaction.response.send_message('Кнопка доступна только на сервере.', ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(interaction.client.form))


class CloseView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Закрыть заявку', style=discord.ButtonStyle.danger, custom_id='clan:close:v1')
    async def close_ticket(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        async with bot.lock:
            ticket = bot.db.execute('SELECT owner, role, status, voice_channel FROM tickets WHERE channel=?', (interaction.channel_id,)).fetchone()
            if not ticket:
                await interaction.followup.send('Этот тикет уже закрыт или не найден.', ephemeral=True)
                return
            member = interaction.user
            if not member.guild_permissions.administrator and ticket[1] not in [r.id for r in member.roles]:
                await interaction.followup.send('Закрыть заявку может только офицер или администратор.', ephemeral=True)
                return
            if ticket[2] == 'closed':
                await interaction.followup.send('Заявка уже закрыта. Можно сохранить её в архив.', view=ArchiveView(), ephemeral=True)
                return
            if ticket[2] != 'open':
                await interaction.followup.send('Заявка уже архивирована.', ephemeral=True)
                return
            # Сначала скрываем канал от заявителя, затем меняем состояние.
            owner = bot.get_user(ticket[0]) or await bot.fetch_user(ticket[0])
            await interaction.channel.set_permissions(owner, overwrite=discord.PermissionOverwrite(view_channel=False, send_messages=False, read_message_history=False), reason='Заявка закрыта')
            await delete_ticket_voice(interaction.guild, ticket[3])
            with bot.db:
                bot.db.execute("UPDATE tickets SET status='closed' WHERE channel=?", (interaction.channel_id,))
            await temporary_confirmation(interaction, 'Заявка закрыта. Переписка сохранена; можно подать новую заявку.')
            await interaction.channel.send(f'Заявка закрыта: {interaction.user.mention}. Нажмите «В архив», чтобы сохранить переписку и удалить канал.', view=ArchiveView())


    @discord.ui.button(label='Успешное собеседование', style=discord.ButtonStyle.success, custom_id='clan:approve:v1')
    async def approve_interview(self, interaction, button):
        await approve(interaction)


class ArchiveView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='В архив', style=discord.ButtonStyle.secondary, custom_id='clan:archive:v1')
    async def archive(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        async with bot.lock:
            ticket = bot.db.execute('SELECT owner, role, status FROM tickets WHERE channel=?', (interaction.channel_id,)).fetchone()
            if not ticket or ticket[2] != 'closed':
                await interaction.followup.send('Сначала закройте заявку.', ephemeral=True)
                return
            if not interaction.user.guild_permissions.administrator and ticket[1] not in [r.id for r in interaction.user.roles]:
                await interaction.followup.send('Архив доступен только рекрутерам и администраторам.', ephemeral=True)
                return
            guild = interaction.guild
            configured = bot.db.execute('SELECT channel FROM archives WHERE guild=?', (guild.id,)).fetchone()
            destination = None
            if configured:
                try:
                    destination = await guild.fetch_channel(configured[0])
                except discord.NotFound:
                    pass
            role = guild.get_role(ticket[1])
            if role is None or role.is_default():
                await interaction.followup.send('Роль рекрутеров не найдена. Канал заявки сохранён.', ephemeral=True)
                return
            if destination is None:
                destination = await guild.create_text_channel('транскрипты', overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                }, reason='Закрытый архив заявок BEDA')
                with bot.db:
                    bot.db.execute('INSERT OR REPLACE INTO archives VALUES (?, ?)', (guild.id, destination.id))
            if not isinstance(destination, discord.TextChannel) or destination.id == interaction.channel_id or destination.permissions_for(guild.default_role).view_channel:
                await interaction.followup.send('Архив должен быть отдельным закрытым текстовым каналом. Заявка сохранена.', ephemeral=True)
                return
            # Always take a fresh snapshot on retries so later messages are included.
            try:
                saved = await export_ticket(interaction.channel, destination, ticket[0], interaction.user)
            except ValueError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            with bot.db:
                bot.db.execute('UPDATE tickets SET archive_channel=?, archive_message=? WHERE channel=?', (destination.id, saved.id, interaction.channel_id))
            await interaction.channel.delete(reason=f'Транскрипт сохранён: {saved.id}')
            with bot.db:
                bot.db.execute("UPDATE tickets SET status='archived' WHERE channel=?", (interaction.channel_id,))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if not os.getenv('DISCORD_TOKEN') or not os.getenv('DISCORD_GUILD_ID', '').isdigit():
        raise SystemExit('Заполните DISCORD_TOKEN и DISCORD_GUILD_ID в файле .env (пример: .env.example).')
    bot = ClanBot()

    @bot.tree.command(name='create_event', description='Создать событие с записью участников')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel='Канал, в котором нужно опубликовать событие')
    async def create_event(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.send_modal(EventModal(channel))

    async def publish_document(interaction, filename, confirmation):
        messages = [part.strip() for part in (ROOT / filename).read_text(encoding='utf-8').split('<!-- message -->') if part.strip()]
        if not messages or any(len(message.encode('utf-16-le')) // 2 > 2000 for message in messages):
            await interaction.response.send_message(f'Проверьте {filename}: каждый раздел должен содержать от 1 до 2000 символов.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = []
        try:
            for message in messages:
                sent.append(await interaction.channel.send(message, allowed_mentions=discord.AllowedMentions.none()))
        except discord.HTTPException:
            for message in sent:
                try:
                    await message.delete()
                except discord.HTTPException:
                    log.exception('Не удалось убрать часть публикации %s', message.id)
            raise
        await temporary_confirmation(interaction, confirmation)

    @bot.tree.command(name='publish_rules', description='Опубликовать правила сообщества BEDA в текущем канале')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish_rules(interaction: discord.Interaction):
        await publish_document(interaction, 'community_rules.md', 'Правила BEDA опубликованы.')

    @bot.tree.command(name='publish_caller', description='Опубликовать обязанности коллера в текущем канале')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish_caller(interaction: discord.Interaction):
        await publish_document(interaction, 'caller_guide.md', 'Обязанности коллера опубликованы.')

    @bot.tree.command(name='setup_tickets', description='Опубликовать панель заявок в текущем канале')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(category='Категория для новых тикетов', officers='Роль офицеров, рассматривающих заявки')
    async def setup(interaction: discord.Interaction, category: discord.CategoryChannel, officers: discord.Role):
        if officers.is_default() or officers.managed:
            await interaction.response.send_message('Выберите обычную роль офицеров, а не @everyone или роль интеграции.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        with bot.db:
            bot.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?, ?)', (interaction.guild_id, category.id, officers.id))
        await interaction.channel.send(embed=discord.Embed(title=bot.form['title'], description=bot.form['description'], color=discord.Color.blue()), view=ApplyView())
        await interaction.followup.send('Панель заявок опубликована.', ephemeral=True)

    @bot.tree.error
    async def command_error(interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message('Эта команда доступна только администратору.', ephemeral=True)
        else:
            await report_error(interaction, error)

    bot.run(os.environ['DISCORD_TOKEN'])


if __name__ == '__main__':
    main()
