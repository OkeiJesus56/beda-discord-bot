import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands
from temp_voice import TempVoice


class TempVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.bot = discord.Client(intents=discord.Intents.none())
        self.bot.db = self.db
        self.bot.tree = app_commands.CommandTree(self.bot)
        self.feature = TempVoice(self.bot)
        self.room = MagicMock(spec=discord.VoiceChannel)
        self.room.id = 9
        self.room.members = []
        self.room.delete = AsyncMock()
        self.guild = MagicMock()
        self.guild.id = 1
        self.guild.unavailable = False
        self.guild.get_channel.return_value = self.room
        self.bot.get_guild = MagicMock(return_value=self.guild)

    def track(self, age=60):
        self.db.execute('INSERT INTO temporary_voice VALUES (9,1,2,?)', (time.time()-age,))
        self.db.commit()

    async def test_untracked_room_never_deleted(self):
        await self.feature.remove_empty(9)
        self.room.delete.assert_not_awaited()

    async def test_empty_tracked_room_deleted(self):
        self.track()
        await self.feature.remove_empty(9)
        self.room.delete.assert_awaited_once()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM temporary_voice').fetchone()[0], 0)

    async def test_occupied_room_survives(self):
        self.track()
        self.room.members = [object()]
        await self.feature.remove_empty(9)
        self.room.delete.assert_not_awaited()

    async def test_new_room_has_grace_period(self):
        self.track(age=0)
        await self.feature.remove_empty(9)
        self.room.delete.assert_not_awaited()

    async def test_saved_room_is_cleaned_after_restart(self):
        self.track()
        self.bot.tree.clear_commands(guild=None)
        fresh = TempVoice(self.bot)
        await fresh.remove_empty(9)
        self.room.delete.assert_awaited_once()

    async def test_join_hub_creates_and_moves(self):
        hub = MagicMock(spec=discord.VoiceChannel)
        hub.id = 10
        category = MagicMock(spec=discord.CategoryChannel)
        category.permissions_for.return_value = discord.Permissions.all()
        self.guild.get_channel.return_value = category
        self.guild.create_voice_channel = AsyncMock(return_value=self.room)
        member = SimpleNamespace(id=2, guild=self.guild, bot=False, display_name='Лев', voice=SimpleNamespace(channel=hub), move_to=AsyncMock())
        self.db.execute('INSERT INTO voice_hubs VALUES (1,10,11)')
        self.db.commit()
        await self.feature.voice_changed(member, SimpleNamespace(channel=None), SimpleNamespace(channel=hub))
        self.guild.create_voice_channel.assert_awaited_once_with('🔊 Лев', category=category, reason='Личная голосовая комната')
        member.move_to.assert_awaited_once()
        self.assertEqual(self.db.execute('SELECT channel FROM temporary_voice').fetchone()[0], 9)
