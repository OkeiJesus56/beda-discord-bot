import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from bot import ApplicationModal, CloseView, ArchiveView, load_form, migrate_tickets


class TicketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE tickets (channel INTEGER, guild INTEGER, owner INTEGER, role INTEGER, status TEXT)')
        self.db.execute("INSERT INTO tickets VALUES (10, 20, 30, 40, 'open')")
        migrate_tickets(self.db)
        self.bot = SimpleNamespace(db=self.db, lock=asyncio.Lock(), get_user=lambda _: discord.Object(id=30))
        self.interaction = SimpleNamespace(
            client=self.bot, channel_id=10,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=30, mention='<@30>', roles=[], guild_permissions=SimpleNamespace(administrator=False)),
            guild=SimpleNamespace(id=20, fetch_channel=AsyncMock()),
            channel=SimpleNamespace(set_permissions=AsyncMock(), send=AsyncMock()),
        )

    async def test_duplicate_returns_existing_ticket(self):
        await ApplicationModal(load_form()).on_submit(self.interaction)
        self.assertIn('<#10>', self.interaction.followup.send.call_args.args[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 1)

    async def test_applicant_cannot_close(self):
        await CloseView().children[0].callback(self.interaction)
        self.interaction.channel.set_permissions.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'open')

    async def test_officer_closes_and_locks_applicant(self):
        self.interaction.user.roles = [SimpleNamespace(id=40)]
        await CloseView().children[0].callback(self.interaction)
        overwrite = self.interaction.channel.set_permissions.call_args.kwargs['overwrite']
        self.assertFalse(overwrite.send_messages)
        self.assertFalse(overwrite.view_channel)
        self.assertFalse(overwrite.read_message_history)
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'closed')

    async def test_permission_failure_keeps_ticket_open(self):
        self.interaction.user.roles = [SimpleNamespace(id=40)]
        self.interaction.channel.set_permissions.side_effect = RuntimeError('permission failure')
        with self.assertRaises(RuntimeError):
            await CloseView().children[0].callback(self.interaction)
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'open')

    async def test_form_has_requested_fields(self):
        form = ApplicationModal(load_form())
        self.assertEqual(len(form.children), 5)
        self.assertTrue(all(item.required for item in form.children))

    async def test_migration_preserves_old_ticket(self):
        migrate_tickets(self.db)
        self.assertEqual(self.db.execute('SELECT channel, status, voice_channel FROM tickets').fetchone(), (10, 'open', None))

    def prepare_archive(self):
        self.db.execute("UPDATE tickets SET status='closed'")
        self.db.execute('INSERT INTO archives VALUES (20, 90)')
        self.interaction.user.roles = [SimpleNamespace(id=40)]
        guild = self.interaction.guild
        guild.default_role = MagicMock()
        role = MagicMock()
        role.is_default.return_value = False
        guild.get_role = MagicMock(return_value=role)
        destination = MagicMock(spec=discord.TextChannel)
        destination.id = 90
        destination.permissions_for.return_value.view_channel = False
        guild.fetch_channel.return_value = destination
        self.interaction.channel.delete = AsyncMock()
        return destination

    async def test_archive_failure_never_deletes_ticket(self):
        self.prepare_archive()
        with patch('bot.export_ticket', new=AsyncMock(side_effect=ValueError('too large'))):
            await ArchiveView().children[0].callback(self.interaction)
        self.interaction.channel.delete.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'closed')

    async def test_archive_saved_before_delete(self):
        self.prepare_archive()
        saved = SimpleNamespace(id=100)
        async def check_delete(**kwargs):
            self.assertEqual(self.db.execute('SELECT archive_message FROM tickets').fetchone()[0], 100)
        self.interaction.channel.delete.side_effect = check_delete
        with patch('bot.export_ticket', new=AsyncMock(return_value=saved)) as export:
            await ArchiveView().children[0].callback(self.interaction)
        export.assert_awaited_once()
        self.interaction.channel.delete.assert_awaited_once()
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'archived')

    async def test_applicant_cannot_archive(self):
        self.prepare_archive()
        self.interaction.user.roles = []
        with patch('bot.export_ticket', new=AsyncMock()) as export:
            await ArchiveView().children[0].callback(self.interaction)
        export.assert_not_awaited()
        self.interaction.channel.delete.assert_not_awaited()

    async def test_close_deletes_voice(self):
        self.db.execute('UPDATE tickets SET voice_channel=50')
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.delete = AsyncMock()
        self.interaction.guild.fetch_channel.return_value = voice
        self.interaction.user.roles = [SimpleNamespace(id=40)]
        await CloseView().children[0].callback(self.interaction)
        self.interaction.guild.fetch_channel.assert_awaited_once_with(50)
        voice.delete.assert_awaited_once()

    async def test_voice_delete_failure_can_be_retried(self):
        self.db.execute('UPDATE tickets SET voice_channel=50')
        self.interaction.guild.fetch_channel.side_effect = RuntimeError('forbidden')
        self.interaction.user.roles = [SimpleNamespace(id=40)]
        with self.assertRaises(RuntimeError):
            await CloseView().children[0].callback(self.interaction)
        self.assertEqual(self.db.execute('SELECT status FROM tickets').fetchone()[0], 'open')

    def prepare_creation(self):
        self.db.execute('DELETE FROM tickets')
        self.db.execute('CREATE TABLE settings (guild INTEGER, category INTEGER, role INTEGER)')
        self.db.execute('INSERT INTO settings VALUES (20, 60, 40)')
        guild = self.interaction.guild
        guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.CategoryChannel))
        role = MagicMock(spec=discord.Role)
        role.id = 40
        role.is_default.return_value = False
        guild.get_role = MagicMock(return_value=role)
        guild.default_role = MagicMock(spec=discord.Role)
        guild.me = MagicMock(spec=discord.Member)
        self.interaction.user = MagicMock(spec=discord.Member)
        self.interaction.user.id = 30
        self.interaction.user.mention = '<@30>'
        self.interaction.user.display_name = 'JesusWhyNot (Лев)'
        self.interaction.user.name = 'jesuswhynot'
        text = MagicMock(spec=discord.TextChannel)
        text.id = 70
        text.send = AsyncMock()
        text.delete = AsyncMock()
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.id = 80
        voice.mention = '<#80>'
        voice.delete = AsyncMock()
        guild.create_text_channel = AsyncMock(return_value=text)
        guild.create_voice_channel = AsyncMock(return_value=voice)
        return guild, text, voice, role

    async def test_creates_private_channel_pair(self):
        guild, text, voice, role = self.prepare_creation()
        await ApplicationModal(load_form()).on_submit(self.interaction)
        self.assertEqual(self.db.execute('SELECT channel, voice_channel FROM tickets').fetchone(), (70, 80))
        self.assertEqual(guild.create_text_channel.call_args.args[0], 'vstyplenie-jesuswhynot-лев')
        self.assertEqual(guild.create_voice_channel.call_args.args[0], 'Собеседование-JesusWhyNot (Лев)')
        for create in (guild.create_text_channel, guild.create_voice_channel):
            overwrites = create.call_args.kwargs['overwrites']
            self.assertFalse(overwrites[guild.default_role].view_channel)
            self.assertTrue(overwrites[self.interaction.user].view_channel)
            self.assertTrue(overwrites[role].view_channel)
        voice_permissions = guild.create_voice_channel.call_args.kwargs['overwrites']
        self.assertTrue(voice_permissions[self.interaction.user].connect)
        self.assertTrue(voice_permissions[role].speak)
        self.assertIn('<#80>', text.send.call_args_list[0].kwargs['embed'].description)

    async def test_voice_creation_failure_rolls_back_text(self):
        guild, text, voice, role = self.prepare_creation()
        guild.create_voice_channel.side_effect = RuntimeError('channel limit')
        with self.assertRaises(RuntimeError):
            await ApplicationModal(load_form()).on_submit(self.interaction)
        text.delete.assert_awaited_once()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)

    async def test_message_failure_rolls_back_both_channels(self):
        guild, text, voice, role = self.prepare_creation()
        text.send.side_effect = RuntimeError('forbidden')
        with self.assertRaises(RuntimeError):
            await ApplicationModal(load_form()).on_submit(self.interaction)
        text.delete.assert_awaited_once()
        voice.delete.assert_awaited_once()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
