import asyncio
import json
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands
from where_play import WherePlay, CallerView, CallerActivityView, CALLER_ACTIVITIES, join_caller, card, find_server, join_code, stale, caller_slot_available


def snapshot():
    return {'data': [{'serverId': '00123', 'id': 'instance-new', 'name': 'Test', 'players': 42, 'maxPlayers': 100, 'map': {'variant': 'TestMap'}}],
            'meta': {'stale': False, 'fetchedAt': datetime.now(timezone.utc).isoformat(), 'refreshSeconds': 60}}


class WhereTests(unittest.IsolatedAsyncioTestCase):
    def test_five_caller_limit_and_return(self):
        rooms = {i: MagicMock(spec=discord.VoiceChannel) for i in range(5)}
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: rooms.get(channel_id))
        self.db.executemany('INSERT INTO caller_rooms VALUES (1, ?, ?)', [(i, i) for i in range(5)])
        self.assertFalse(caller_slot_available(self.db, guild, 10))
        self.assertTrue(caller_slot_available(self.db, guild, 0))
        del rooms[0]
        self.assertTrue(caller_slot_available(self.db, guild, 10))

    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.bot = discord.Client(intents=discord.Intents.none())
        self.bot.db = self.db
        self.bot.tree = app_commands.CommandTree(self.bot)
        self.feature = WherePlay(self.bot)
        self.feature.api.get = AsyncMock(return_value=snapshot())
        self.feature.publish = AsyncMock()
        self.db.execute("INSERT INTO where_play VALUES (1,2,3,4,'00123','Синие',5,1,NULL)")
        self.db.commit()
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=5, roles=[SimpleNamespace(id=3)], guild_permissions=SimpleNamespace(administrator=False)),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def test_refresh_uses_join_code_after_restart(self):
        await self.feature.refresh()
        self.assertEqual(json.loads(self.feature.row(1)[8])['server']['id'], 'instance-new')
        self.assertIn('42 / 100', str(self.feature.publish.call_args.args[1].to_dict()))

    async def test_missing_server_keeps_previous_data(self):
        await self.feature.refresh()
        old = self.feature.row(1)[8]
        data = snapshot()
        data['data'] = []
        self.feature.api.get.return_value = data
        await self.feature.refresh()
        self.assertEqual(self.feature.row(1)[8], old)
        self.assertIn('отсутствует', self.feature.publish.call_args.args[1].description)

    async def test_network_failure_shows_warning(self):
        self.feature.api.get.side_effect = ValueError('offline')
        await self.feature.refresh()
        self.assertIn('offline', self.feature.publish.call_args.args[1].description)

    async def test_stale_response_still_updates_last_known_values(self):
        data = snapshot()
        data['meta']['stale'] = True
        self.feature.api.get.return_value = data
        await self.feature.refresh()
        self.assertEqual(json.loads(self.feature.row(1)[8])['server']['players'], 42)
        self.assertIn('устаревшие', self.feature.publish.call_args.args[1].description)

    async def test_unknown_code_does_not_change_selection(self):
        await self.bot.tree.get_command('where_play').callback(self.interaction, '99999', '🔴 Красные')
        self.assertEqual(self.feature.row(1)[4], '00123')
        self.feature.publish.assert_not_awaited()

    async def test_caller_can_switch_and_stop(self):
        await self.bot.tree.get_command('where_play').callback(self.interaction, '00123', '🔴 Красные')
        self.assertEqual(self.feature.row(1)[5], '🔴 Красные')
        await self.bot.tree.get_command('stop_play').callback(self.interaction)
        self.assertEqual(self.feature.row(1)[7], 0)
        self.feature.api.get.reset_mock()
        await self.feature.refresh()
        self.feature.api.get.assert_not_awaited()

    async def test_member_cannot_change_card(self):
        self.interaction.user.roles = []
        await self.bot.tree.get_command('where_play').callback(self.interaction, '00123', '🔴 Красные')
        self.feature.api.get.assert_not_awaited()
        self.feature.publish.assert_not_awaited()

    async def test_failed_discord_edit_keeps_old_selection(self):
        self.feature.publish.side_effect = RuntimeError('forbidden')
        with self.assertRaises(RuntimeError):
            await self.bot.tree.get_command('where_play').callback(self.interaction, '00123', '🔴 Красные')
        self.assertEqual(self.feature.row(1)[5], 'Синие')

    def test_stale_by_age_even_if_flag_false(self):
        data = snapshot()
        data['meta']['fetchedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertTrue(stale(data))

    def test_code_is_exact_and_preserves_zeros(self):
        self.assertEqual(join_code(' 00123 '), '00123')
        self.assertIsNone(find_server(snapshot(), '123'))
        with self.assertRaises(ValueError):
            join_code('Official #12')

    def test_team_choices_have_colors(self):
        command = self.bot.tree.get_command('where_play')
        parameter = next(p for p in command.parameters if p.name == 'team')
        self.assertEqual([c.value for c in parameter.choices], ['🔵 Синие', '🔴 Красные', '🟢 Зелёные'])

    async def test_caller_button_denies_unassigned_role(self):
        self.bot.where_play = self.feature
        self.interaction.client = self.bot
        self.interaction.message = SimpleNamespace(id=4)
        self.interaction.user.roles = []
        await CallerView().children[0].callback(self.interaction)
        self.assertIn('только роли', self.interaction.followup.send.call_args.args[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM caller_rooms').fetchone()[0], 0)

    async def test_caller_must_be_connected(self):
        self.bot.where_play = self.feature
        self.interaction.client = self.bot
        self.interaction.message = SimpleNamespace(id=4)
        self.interaction.user.voice = None
        await CallerView().children[0].callback(self.interaction)
        self.assertIn('Сначала зайдите', self.interaction.followup.send.call_args.args[0])
        self.assertTrue(CallerView().is_persistent())

    def prepare_activity(self):
        self.bot.where_play = self.feature
        self.bot.temp_voice = SimpleNamespace(lock=asyncio.Lock())
        self.interaction.client = self.bot
        self.interaction.message = SimpleNamespace(id=4, embeds=[card(self.feature.row(1))])
        self.interaction.user.voice = SimpleNamespace(channel=SimpleNamespace(id=9))
        self.interaction.user.display_name = 'Игрок'
        self.interaction.user.move_to = AsyncMock()
        self.db.execute('CREATE TABLE voice_hubs (guild INTEGER, hub INTEGER, category INTEGER)')
        self.db.execute('INSERT INTO voice_hubs VALUES (1, 9, 10)')
        self.db.execute('CREATE TABLE temporary_voice (channel INTEGER PRIMARY KEY, guild INTEGER, owner INTEGER, created REAL)')
        category = MagicMock(spec=discord.CategoryChannel)
        category.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True)
        room = MagicMock(spec=discord.VoiceChannel)
        room.id, room.name, room.mention = 11, 'Старая комната', '<#11>'
        room.members = [self.interaction.user]
        room.edit = AsyncMock()
        room.delete = AsyncMock()
        self.rooms = {10: category}
        self.interaction.guild = SimpleNamespace(id=1, get_channel=lambda cid: self.rooms.get(cid),
                                                 create_voice_channel=AsyncMock(return_value=room))
        return room

    async def test_join_only_opens_activity_menu(self):
        self.prepare_activity()
        await CallerView().children[0].callback(self.interaction)
        payload = self.interaction.followup.send.call_args.kwargs
        self.assertTrue(payload['ephemeral'])
        self.assertIsInstance(payload['view'], CallerActivityView)
        self.assertEqual([o.value for o in payload['view'].children[0].options], list(CALLER_ACTIVITIES))
        self.interaction.guild.create_voice_channel.assert_not_awaited()
        self.interaction.user.move_to.assert_not_awaited()

    async def test_each_activity_names_room_and_tracks_caller(self):
        room = self.prepare_activity()
        for activity in CALLER_ACTIVITIES:
            self.db.execute('DELETE FROM temporary_voice')
            self.db.execute('DELETE FROM caller_rooms')
            await join_caller(self.interaction, self.interaction.message, activity)
            self.assertEqual(self.interaction.guild.create_voice_channel.call_args.args[0], f'{activity} - Игрок')
            self.interaction.user.move_to.assert_awaited_with(room, reason='Я коллер — присоединение к комнате')
            self.assertEqual(self.db.execute('SELECT member, channel FROM caller_rooms').fetchone(), (5, 11))
            self.feature.publish.assert_awaited()

    async def test_selection_uses_original_card_id(self):
        self.prepare_activity()
        view = CallerActivityView(5, self.interaction.message)
        self.interaction.message = SimpleNamespace(id=999, embeds=[])
        view.children[0]._values = ['ДРГ']
        await view.children[0].callback(self.interaction)
        self.interaction.guild.create_voice_channel.assert_awaited_once()

    async def test_existing_room_is_renamed_and_reused(self):
        room = self.prepare_activity()
        self.rooms[11] = room
        self.db.execute('INSERT INTO temporary_voice VALUES (11,1,5,0)')
        await join_caller(self.interaction, self.interaction.message, 'Разведка')
        room.edit.assert_awaited_with(name='Разведка - Игрок', reason='Выбор деятельности коллера')
        self.interaction.guild.create_voice_channel.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM temporary_voice').fetchone()[0], 1)

    async def test_selection_rechecks_active_card_and_role(self):
        self.prepare_activity()
        self.db.execute('UPDATE where_play SET active=0')
        await join_caller(self.interaction, self.interaction.message, 'Штурм')
        self.interaction.guild.create_voice_channel.assert_not_awaited()
        self.db.execute('UPDATE where_play SET active=1')
        self.interaction.user.roles = []
        await join_caller(self.interaction, self.interaction.message, 'Стройка')
        self.interaction.guild.create_voice_channel.assert_not_awaited()

    async def test_roster_still_contains_member_and_room_link(self):
        room = self.prepare_activity()
        self.rooms[11] = room
        self.db.execute('INSERT INTO caller_rooms VALUES (1,5,11)')
        self.bot.get_guild = lambda _: self.interaction.guild
        message = SimpleNamespace(edit=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(get_partial_message=lambda _: message))
        await WherePlay.publish(self.feature, self.feature.row(1), card(self.feature.row(1)))
        fields = message.edit.call_args.kwargs['embed'].fields
        self.assertEqual(next(f.value for f in fields if f.name == '🎖️ Коллеры'), '<@5> — <#11>')
