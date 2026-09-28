"""Persistent admission workflow and extensible personal dossiers."""
import json
from pathlib import Path

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parent
SPECIALIZATIONS = ('Штурмовик', 'Медик', 'Механик', 'Строитель', 'Пилот', 'Снайпер')


def init_ticket_applications(db):
    db.execute('CREATE TABLE IF NOT EXISTS ticket_applications (channel INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    db.commit()


def init_dossiers(db):
    init_ticket_applications(db)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS dossier_settings (
            guild INTEGER PRIMARY KEY, forum INTEGER NOT NULL, roles TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admissions (
            guild INTEGER, owner INTEGER, ticket INTEGER NOT NULL,
            PRIMARY KEY (guild, owner));
        CREATE TABLE IF NOT EXISTS dossiers (
            guild INTEGER, owner INTEGER, data TEXT NOT NULL,
            thread INTEGER, status TEXT NOT NULL,
            PRIMARY KEY (guild, owner));
        CREATE TABLE IF NOT EXISTS dossier_drafts (
            guild INTEGER, owner INTEGER, ticket INTEGER NOT NULL, data TEXT NOT NULL,
            revision INTEGER NOT NULL, PRIMARY KEY (guild, owner));
    ''')


def clean(value):
    return ' '.join(value.split())


def validate(data, require_source=True):
    for key, label, maximum in (('nickname', 'Ник', 60), ('name', 'Имя', 60),
                                ('city', 'Город', 60), ('steam_id', 'Steam ID', 100)):
        data[key] = clean(data.get(key, ''))
        if not require_source and key in ('city', 'steam_id') and not data[key]:
            continue
        if not 1 <= len(data[key]) <= maximum:
            raise ValueError(f'{label}: требуется от 1 до {maximum} символов.')
    data.pop('timezone', None)
    if data.get('primary') not in SPECIALIZATIONS or data.get('secondary') not in SPECIALIZATIONS:
        raise ValueError('Выберите обе специализации.')
    if data['primary'] == data['secondary']:
        raise ValueError('Основная и дополнительная специализации должны различаться.')
    return data


def dossier_card(data, owner):
    """Native Discord text fields with an unmodified specialization attachment."""
    data = validate(dict(data))
    image_path = ROOT / 'assets' / 'specializations' / f"{data['primary']}.png"
    embed = discord.Embed(title='BEDA / ЛИЧНОЕ ДЕЛО',
                          description=f'Участник: <@{owner}>', color=0xB89960)
    for key, label in (('nickname', 'Ник'), ('name', 'Имя'), ('steam_id', 'Steam ID'),
                       ('city', 'Город'), ('primary', 'Основная специализация'),
                       ('secondary', 'Дополнительная специализация')):
        embed.add_field(name=label, value=discord.utils.escape_markdown(data[key]), inline=False)
    embed.set_image(url='attachment://specialization.png')
    return embed, image_path


class DossierView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        from bot import report_error
        await report_error(interaction, error)


class AdmissionView(DossierView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Заполнить личное дело', style=discord.ButtonStyle.primary,
                       custom_id='clan:dossier:form:v1')
    async def fill(self, interaction, button):
        row = interaction.client.db.execute(
            'SELECT ticket FROM admissions WHERE guild=? AND owner=?',
            (interaction.guild_id, interaction.user.id)).fetchone()
        if not row or row[0] != interaction.channel_id:
            await interaction.response.send_message('Анкета доступна только одобренному заявителю в его тикете.', ephemeral=True)
            return
        await interaction.response.send_modal(DossierModal())


async def approve(interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('Одобрить собеседование может только администратор.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    async with bot.lock:
        ticket = bot.db.execute("SELECT owner FROM tickets WHERE guild=? AND channel=? AND status='open'",
                                (interaction.guild_id, interaction.channel_id)).fetchone()
        if not ticket:
            await interaction.followup.send('Нужен открытый тикет.', ephemeral=True)
            return
        if not bot.db.execute('SELECT 1 FROM dossier_settings WHERE guild=?', (interaction.guild_id,)).fetchone():
            await interaction.followup.send('Сначала выполните /setup_dossiers.', ephemeral=True)
            return
        with bot.db:
            bot.db.execute('INSERT OR REPLACE INTO admissions VALUES (?, ?, ?)',
                           (interaction.guild_id, ticket[0], interaction.channel_id))
        await interaction.channel.send(f'<@{ticket[0]}>, собеседование пройдено. Заполните личное дело.',
                                       view=AdmissionView(), allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send('Анкета доступна заявителю по кнопке в тикете.', ephemeral=True)


class DossierModal(discord.ui.Modal, title='Личное дело'):
    nickname = discord.ui.TextInput(label='Ник', max_length=60)
    name = discord.ui.TextInput(label='Имя', max_length=60)

    async def on_error(self, interaction, error):
        from bot import report_error
        await report_error(interaction, error)

    async def on_submit(self, interaction):
        data = {key: str(getattr(self, key)) for key in ('nickname', 'name')}
        await interaction.response.send_message('Выберите две специализации и отправьте анкету администратору. Steam ID и город будут взяты из исходной заявки.',
                                                view=SpecializationView(data), ephemeral=True)


class SpecializationSelect(discord.ui.Select):
    def __init__(self, key, label):
        super().__init__(placeholder=label, options=[discord.SelectOption(label=s) for s in SPECIALIZATIONS])
        self.key = key

    async def callback(self, interaction):
        self.view.data[self.key] = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(view=self.view)


class SpecializationView(DossierView):
    def __init__(self, data):
        super().__init__(timeout=900)
        self.data = data
        self.add_item(SpecializationSelect('primary', 'Основная специализация'))
        self.add_item(SpecializationSelect('secondary', 'Дополнительная специализация'))

    @discord.ui.button(label='Отправить анкету', style=discord.ButtonStyle.success, row=2)
    async def submit(self, interaction, button):
        try:
            data = validate(dict(self.data), require_source=False)
        except (ValueError, KeyError) as error:
            await interaction.response.send_message(str(error) if isinstance(error, ValueError) else 'Выберите обе специализации.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await submit_draft(interaction, data)


async def ticket_source(bot, channel):
    """Read persisted answers, or recover the original bot embed for older tickets."""
    row = bot.db.execute('SELECT data FROM ticket_applications WHERE channel=?', (channel.id,)).fetchone()
    if row:
        answers = json.loads(row[0])
    else:
        answers = {}
        async for message in channel.history(limit=None, oldest_first=True):
            if message.author.id != bot.user.id:
                continue
            for embed in message.embeds:
                fields = {field.name: field.value for field in embed.fields}
                if 'Steam ID' in fields:
                    answers = fields
                    break
            if answers:
                break
        if answers:
            with bot.db:
                bot.db.execute('INSERT OR IGNORE INTO ticket_applications VALUES (?, ?)',
                               (channel.id, json.dumps(answers, ensure_ascii=False)))
    return {'steam_id': clean(answers.get('steam_id', answers.get('Steam ID', ''))),
            'city': clean(answers.get('city', answers.get('Город', '')))}


def admitted(bot, guild_id, owner, ticket):
    return bot.db.execute('''SELECT 1 FROM admissions a JOIN tickets t ON t.channel=a.ticket
        AND t.guild=a.guild AND t.owner=a.owner
        WHERE a.guild=? AND a.owner=? AND a.ticket=? AND t.status='open' ''',
        (guild_id, owner, ticket)).fetchone() is not None


async def submit_draft(interaction, data):
    bot = interaction.client
    key = (interaction.guild_id, interaction.user.id)
    async with bot.lock:
        if not admitted(bot, *key, interaction.channel_id):
            await interaction.followup.send('Заполнить анкету можно только в своём открытом тикете после одобрения.', ephemeral=True)
            return
        if bot.db.execute('SELECT 1 FROM dossiers WHERE guild=? AND owner=?', key).fetchone():
            await interaction.followup.send('Личное дело уже создано или находится в процессе публикации. Обратитесь к администратору.', ephemeral=True)
            return
        source = await ticket_source(bot, interaction.channel)
        try:
            data = validate({**data, **source}, require_source=False)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        with bot.db:
            bot.db.execute('''INSERT INTO dossier_drafts VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(guild, owner) DO UPDATE SET ticket=excluded.ticket,
                data=excluded.data, revision=dossier_drafts.revision+1''',
                (*key, interaction.channel_id, json.dumps(data, ensure_ascii=False)))
    # No creation button is ever attached to a public or applicant-facing message.
    await interaction.followup.send('Анкета сохранена и передана на проверку администратору.', ephemeral=True)
    await interaction.channel.send('Анкета личного дела заполнена. Администратор: откройте /review_dossier в этом тикете.',
                                   allowed_mentions=discord.AllowedMentions.none())


class AdminCreateView(DossierView):
    def __init__(self, owner, revision):
        super().__init__(timeout=900)
        self.owner = owner
        self.revision = revision

    @discord.ui.button(label='Создать личное дело', style=discord.ButtonStyle.success)
    async def create(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await publish_dossier(interaction, self.owner, self.revision)


async def review_draft(interaction, city=None, steam_id=None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('Проверка анкеты доступна только администратору.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    bot = interaction.client
    async with bot.lock:
        row = bot.db.execute('''SELECT d.owner, d.data, d.revision FROM dossier_drafts d
            JOIN tickets t ON t.channel=d.ticket AND t.guild=d.guild AND t.owner=d.owner
            WHERE d.guild=? AND d.ticket=? AND t.status='open' ''',
            (interaction.guild_id, interaction.channel_id)).fetchone()
        if not row:
            await interaction.followup.send('В этом открытом тикете ещё нет заполненной анкеты.', ephemeral=True)
            return
        owner, saved, revision = row
        data = json.loads(saved)
        existing = bot.db.execute('SELECT data FROM dossiers WHERE guild=? AND owner=?', (interaction.guild_id, owner)).fetchone()
        if existing:
            if city is not None or steam_id is not None:
                await interaction.followup.send('Публикация уже началась: её данные менять нельзя.', ephemeral=True)
                return
            data = json.loads(existing[0])
        else:
            for key, value in (('city', city), ('steam_id', steam_id)):
                if value is not None:
                    data[key] = value
            try:
                data = validate(data, require_source=False)
            except ValueError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            if city is not None or steam_id is not None:
                revision += 1
                with bot.db:
                    bot.db.execute('UPDATE dossier_drafts SET data=?, revision=? WHERE guild=? AND owner=?',
                                   (json.dumps(data, ensure_ascii=False), revision, interaction.guild_id, owner))
        embed = discord.Embed(title='Проверка личного дела', description=f'Заявитель: <@{owner}>', color=discord.Color.blue())
        for key, label in (('nickname', 'Ник'), ('name', 'Имя'), ('steam_id', 'Steam ID'),
                           ('city', 'Город'), ('primary', 'Основная специализация'), ('secondary', 'Дополнительная специализация')):
            embed.add_field(name=label, value=discord.utils.escape_markdown(data.get(key) or 'Нет в старой заявке'), inline=False)
        missing = [key for key in ('city', 'steam_id') if not data.get(key)] if not existing else []
        if missing:
            await interaction.followup.send('Дополните старую заявку командой /review_dossier с параметрами: ' + ', '.join(missing),
                                           embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=AdminCreateView(owner, revision), ephemeral=True)


def resolve_roles(guild, mapping, data=None):
    names = ['BEDA', *(SPECIALIZATIONS if data is None else (data['primary'], data['secondary']))]
    roles = [guild.get_role(mapping[name]) for name in names]
    if not guild.me.guild_permissions.manage_roles or any(
            role is None or role.is_default() or role.managed or role >= guild.me.top_role for role in roles):
        raise ValueError('Боту нужны «Управлять ролями» и роль выше BEDA и всех специализаций. Проверьте /setup_dossiers.')
    return roles


async def publish_dossier(interaction, owner, revision):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send('Создать личное дело может только администратор.', ephemeral=True)
        return
    bot, guild = interaction.client, interaction.guild
    key = (guild.id, owner)
    async with bot.lock:
        if not admitted(bot, *key, interaction.channel_id):
            await interaction.followup.send('Собеседование ещё не одобрено.', ephemeral=True)
            return
        draft = bot.db.execute('SELECT data, revision, ticket FROM dossier_drafts WHERE guild=? AND owner=?', key).fetchone()
        if not draft or draft[1] != revision or draft[2] != interaction.channel_id:
            await interaction.followup.send('Анкета изменилась. Откройте /review_dossier заново.', ephemeral=True)
            return
        data = json.loads(draft[0])
        existing = bot.db.execute('SELECT data, thread, status FROM dossiers WHERE guild=? AND owner=?', key).fetchone()
        if existing and existing[2] == 'ready':
            await interaction.followup.send(f'Личное дело уже существует: <#{existing[1]}>', ephemeral=True)
            return
        if existing and existing[1] is None:
            await interaction.followup.send('Предыдущая публикация не завершена. Администратору нужно сверить форум и запись dossiers в базе перед повтором, чтобы исключить дубль.', ephemeral=True)
            return
        settings = bot.db.execute('SELECT forum, roles FROM dossier_settings WHERE guild=?', (guild.id,)).fetchone()
        if not settings:
            await interaction.followup.send('Сначала выполните /setup_dossiers.', ephemeral=True)
            return
        if existing:
            data = json.loads(existing[0])
        else:
            try:
                data = validate(data)
            except ValueError as error:
                await interaction.followup.send(str(error) + ' Проверьте /review_dossier.', ephemeral=True)
                return
        try:
            roles = resolve_roles(guild, json.loads(settings[1]), data)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if not existing:
            forum = await guild.fetch_channel(settings[0])
            if not isinstance(forum, discord.ForumChannel) or forum.flags.require_tag:
                await interaction.followup.send('Выберите форум без обязательного тега через /setup_dossiers.', ephemeral=True)
                return
            embed, image_path = dossier_card(data, owner)
            if image_path.stat().st_size > guild.filesize_limit:
                await interaction.followup.send('Изображение превышает лимит вложений сервера.', ephemeral=True)
                return
            attachment = discord.File(image_path, filename='specialization.png')
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO dossiers VALUES (?, ?, ?, NULL, 'publishing')", (*key, json.dumps(data, ensure_ascii=False)))
                # Reserve before the remote operation: ambiguous timeouts must not create duplicate topics.
                try:
                    result = await forum.create_thread(name=f"Личное дело — {data['nickname']}",
                        embed=embed, file=attachment,
                        allowed_mentions=discord.AllowedMentions.none())
                except (discord.Forbidden, discord.NotFound):
                    with bot.db:
                        bot.db.execute('DELETE FROM dossiers WHERE guild=? AND owner=?', key)
                    raise
            finally:
                attachment.close()
            thread_id = result.thread.id
            with bot.db:
                bot.db.execute("UPDATE dossiers SET thread=?, status='roles_pending' WHERE guild=? AND owner=?", (thread_id, *key))
        else:
            thread_id = existing[1]
        member = await guild.fetch_member(key[1])
        await member.add_roles(*roles, reason='Успешное собеседование и создание личного дела')
        with bot.db:
            bot.db.execute("UPDATE dossiers SET status='ready' WHERE guild=? AND owner=?", key)
        await interaction.followup.send(f'Личное дело создано: <#{thread_id}>. Роли выданы.', ephemeral=True)


def register_commands(bot):
    @bot.tree.command(name='setup_dossiers', description='Настроить форум личных дел и роли')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(interaction: discord.Interaction, forum: discord.ForumChannel, beda: discord.Role,
                    assault: discord.Role, medic: discord.Role, mechanic: discord.Role,
                    builder: discord.Role, pilot: discord.Role, sniper: discord.Role):
        mapping = dict(zip(('BEDA', *SPECIALIZATIONS), (r.id for r in (beda, assault, medic, mechanic, builder, pilot, sniper))))
        try:
            resolve_roles(interaction.guild, mapping)
            if len(set(mapping.values())) != 7:
                raise ValueError('Для BEDA и каждой специализации выберите отдельную роль.')
            if forum.flags.require_tag:
                raise ValueError('Отключите обязательный тег в настройках форума.')
            permissions = forum.permissions_for(interaction.guild.me)
            if not all(getattr(permissions, name) for name in ('view_channel', 'send_messages', 'send_messages_in_threads', 'attach_files')):
                raise ValueError('Боту нужен доступ к форуму, создание публикаций, отправка сообщений в ветках и вложений.')
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        with bot.db:
            bot.db.execute('INSERT OR REPLACE INTO dossier_settings VALUES (?, ?, ?)',
                           (interaction.guild_id, forum.id, json.dumps(mapping)))
        await interaction.response.send_message('Форум личных дел настроен.', ephemeral=True)

    @bot.tree.command(name='approve_interview', description='Открыть анкету личного дела в текущем тикете')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def approve_command(interaction: discord.Interaction):
        await approve(interaction)

    @bot.tree.command(name='review_dossier', description='Проверить анкету и создать личное дело заявителя')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(city='Уточнить город, если его нет в старой заявке',
                           steam_id='Уточнить Steam ID из старой заявки')
    async def review_command(interaction: discord.Interaction, city: str = None, steam_id: str = None):
        await review_draft(interaction, city, steam_id)
