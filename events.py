"""Persistent event sign-ups with one response per member."""
import logging

import discord

CHOICES = {'yes': '✅ Участвую', 'no': '❌ Не участвую', 'maybe': '❓ Под вопросом'}
EVENT_ROLE_NAME = '◖🌈◗ BEDA'
log = logging.getLogger('clan_bot.events')


def init_events(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS events (
            message INTEGER PRIMARY KEY, guild INTEGER NOT NULL, channel INTEGER NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, creator INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_answers (
            message INTEGER NOT NULL, member INTEGER NOT NULL,
            choice TEXT NOT NULL CHECK(choice IN ('yes', 'no', 'maybe')),
            PRIMARY KEY(message, member)
        );
    ''')


def event_embed(title, description, answers):
    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    for choice, label in CHOICES.items():
        members = [member for member, selected in answers if selected == choice]
        lines = []
        for member in members:
            line = f'<@{member}>'
            if len('\n'.join(lines + [line])) > 950:
                break
            lines.append(line)
        hidden = len(members) - len(lines)
        if hidden:
            lines.append(f'…и ещё {hidden}')
        embed.add_field(name=f'{label} ({len(members)})', value='\n'.join(lines) or '—', inline=True)
    embed.set_footer(text='Выберите один ответ ниже. Его можно изменить другой кнопкой.')
    return embed


class EventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction, error, item):
        log.error('Ошибка ответа на событие', exc_info=(type(error), error, error.__traceback__))
        text = 'Не удалось обновить ответ. Попробуйте ещё раз; подробности в консоли бота.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def vote(self, interaction, choice):
        await interaction.response.defer()
        bot = interaction.client
        async with bot.lock:
            event = bot.db.execute('SELECT guild, channel, title, description FROM events WHERE message=?', (interaction.message.id,)).fetchone()
            if not event or (event[0], event[1]) != (interaction.guild_id, interaction.channel_id):
                await interaction.followup.send('Событие не найдено.', ephemeral=True)
                return
            # Roll back the choice if Discord rejects the message update.
            with bot.db:
                bot.db.execute('INSERT OR REPLACE INTO event_answers VALUES (?, ?, ?)', (interaction.message.id, interaction.user.id, choice))
                answers = bot.db.execute('SELECT member, choice FROM event_answers WHERE message=? ORDER BY rowid', (interaction.message.id,)).fetchall()
                await interaction.message.edit(embed=event_embed(event[2], event[3], answers), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Участвую', emoji='✅', style=discord.ButtonStyle.success, custom_id='beda:event:yes:v1')
    async def yes(self, interaction, button):
        await self.vote(interaction, 'yes')

    @discord.ui.button(label='Не участвую', emoji='❌', style=discord.ButtonStyle.danger, custom_id='beda:event:no:v1')
    async def no(self, interaction, button):
        await self.vote(interaction, 'no')

    @discord.ui.button(label='Под вопросом', emoji='❓', style=discord.ButtonStyle.secondary, custom_id='beda:event:maybe:v1')
    async def maybe(self, interaction, button):
        await self.vote(interaction, 'maybe')


class EventModal(discord.ui.Modal, title='Новое событие BEDA'):
    def __init__(self, destination: discord.TextChannel):
        super().__init__()
        self.destination = destination

    heading = discord.ui.TextInput(label='Заголовок события', max_length=200, placeholder='Например: 28.09 — тренировка, 20:00 МСК')
    information = discord.ui.TextInput(label='Информация о событии', style=discord.TextStyle.paragraph, max_length=2000,
        placeholder='Время, сервер, план игры и другая важная информация')

    async def on_error(self, interaction, error):
        log.error('Ошибка публикации события', exc_info=(type(error), error, error.__traceback__))
        text = 'Не удалось опубликовать событие. Проверьте права бота: просмотр канала, отправка сообщений и встраивание ссылок.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def on_submit(self, interaction):
        if not interaction.guild or not interaction.permissions.administrator:
            await interaction.response.send_message('Создавать события может только администратор.', ephemeral=True)
            return
        title, description = self.heading.value.strip(), self.information.value.strip()
        if not title or not description:
            await interaction.response.send_message('Заголовок и описание не должны быть пустыми.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        destination = self.destination
        if destination.guild.id != interaction.guild_id:
            await interaction.followup.send('Выберите канал на этом сервере.', ephemeral=True)
            return
        permissions = destination.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            await interaction.followup.send(f'В {destination.mention} боту нужны права: «Просматривать канал», «Отправлять сообщения» и «Встраивать ссылки».', ephemeral=True)
            return
        roles = [role for role in interaction.guild.roles if role.name == EVENT_ROLE_NAME]
        if len(roles) != 1:
            await interaction.followup.send(f'Нужна ровно одна роль с названием «{EVENT_ROLE_NAME}». Найдено: {len(roles)}. Событие не опубликовано.', ephemeral=True)
            return
        notify_role = roles[0]
        if not notify_role.mentionable and not permissions.mention_everyone:
            await interaction.followup.send(f'Бот не может уведомить роль «{EVENT_ROLE_NAME}». Разрешите упоминание этой роли или выдайте боту право «Упоминать @everyone, @here и все роли» в канале события.', ephemeral=True)
            return
        async with bot.lock:
            message = await destination.send(
                content=f'{notify_role.mention} 📅 Новое событие!',
                embed=event_embed(title, description, []),
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[notify_role], replied_user=False),
            )
            with bot.db:
                bot.db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)', (message.id, interaction.guild_id, destination.id, title, description, interaction.user.id))
            try:
                await message.edit(view=EventView(), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                with bot.db:
                    bot.db.execute('DELETE FROM events WHERE message=?', (message.id,))
                await message.delete()
                raise
        confirmation = await interaction.followup.send(f'Событие опубликовано в {destination.mention}.', ephemeral=True, wait=True)
        await confirmation.delete(delay=5)
