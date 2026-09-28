import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from events import EventView, event_embed, init_events


class EventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        init_events(self.db)
        self.db.execute("INSERT INTO events VALUES (1, 2, 3, 'Тренировка', 'Описание', 4)")
        self.db.commit()
        self.interaction = SimpleNamespace(
            client=SimpleNamespace(db=self.db, lock=asyncio.Lock()),
            guild_id=2, channel_id=3, user=SimpleNamespace(id=5),
            message=SimpleNamespace(id=1, edit=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def test_change_moves_member_between_columns(self):
        view = EventView()
        await view.vote(self.interaction, 'yes')
        await view.vote(self.interaction, 'no')
        await view.vote(self.interaction, 'no')
        self.assertEqual(self.db.execute('SELECT member, choice FROM event_answers').fetchall(), [(5, 'no')])
        embed = self.interaction.message.edit.call_args.kwargs['embed']
        self.assertEqual(embed.fields[0].value, '—')
        self.assertEqual(embed.fields[1].value, '<@5>')

    async def test_failed_edit_preserves_previous_answer(self):
        view = EventView()
        await view.vote(self.interaction, 'yes')
        self.interaction.message.edit.side_effect = RuntimeError('Forbidden')
        with self.assertRaises(RuntimeError):
            await view.vote(self.interaction, 'no')
        self.assertEqual(self.db.execute('SELECT choice FROM event_answers').fetchone()[0], 'yes')

    async def test_view_after_restart_uses_saved_event(self):
        await EventView().vote(self.interaction, 'maybe')
        await EventView().vote(self.interaction, 'yes')
        self.assertEqual(self.db.execute('SELECT choice FROM event_answers').fetchone()[0], 'yes')
        self.assertTrue(EventView().is_persistent())

    async def test_unknown_event_does_not_save_vote(self):
        self.interaction.message.id = 999
        await EventView().vote(self.interaction, 'yes')
        self.interaction.message.edit.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event_answers').fetchone()[0], 0)

    def test_large_roster_fits_discord_limits(self):
        embed = event_embed('Тест', 'Описание', [(10**18+i, 'yes') for i in range(1000)])
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertIn('(1000)', embed.fields[0].name)
        self.assertIn('ещё', embed.fields[0].value)
