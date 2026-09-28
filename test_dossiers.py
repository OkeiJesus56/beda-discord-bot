import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from dossiers import (AdmissionView, SPECIALIZATIONS, approve, init_dossiers,
                      publish_dossier, dossier_card, validate, DossierModal,
                      SpecializationView, AdminCreateView, submit_draft, review_draft, ticket_source)


def sample():
    return dict(nickname='Игрок', name='Александр', city='Екатеринбург',
                steam_id='76561198000000000', primary='Медик', secondary='Пилот')


class CardTests(unittest.TestCase):
    def test_validation(self):
        self.assertNotIn('timezone', validate({**sample(), 'timezone': '+2'}))
        self.assertEqual(validate(sample())['steam_id'], sample()['steam_id'])
        with self.assertRaises(ValueError):
            validate({**sample(), 'steam_id': ''})
        with self.assertRaises(ValueError):
            validate({**sample(), 'secondary': 'Медик'})
        with self.assertRaises(ValueError):
            validate({**sample(), 'name': '  '})

    def test_all_six_cards_use_text_fields_and_original_asset(self):
        for specialization in SPECIALIZATIONS:
            data = {**sample(), 'primary': specialization,
                    'secondary': 'Снайпер' if specialization != 'Снайпер' else 'Медик'}
            embed, path = dossier_card(data, 3)
            self.assertEqual(path.name, specialization + '.png')
            self.assertTrue(path.is_file())
            self.assertEqual(embed.image.url, 'attachment://specialization.png')
            fields = {field.name: field.value for field in embed.fields}
            self.assertEqual(fields, {'Ник': 'Игрок', 'Имя': 'Александр',
                'Steam ID': sample()['steam_id'], 'Город': 'Екатеринбург',
                'Основная специализация': specialization,
                'Дополнительная специализация': data['secondary']})


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        init_dossiers(self.db)
        self.db.execute('CREATE TABLE tickets (guild INTEGER, channel INTEGER, owner INTEGER, status TEXT)')
        self.db.execute("INSERT INTO tickets VALUES (1, 2, 3, 'open')")
        self.db.execute('INSERT INTO dossier_settings VALUES (1, 9, ?)', (json.dumps({}),))
        self.db.execute('INSERT INTO admissions VALUES (1, 3, 2)')
        self.db.execute('INSERT INTO ticket_applications VALUES (2, ?)',
                        (json.dumps({'steam_id': sample()['steam_id'], 'city': sample()['city']}),))
        self.db.execute('INSERT INTO dossier_drafts VALUES (1, 3, 2, ?, 1)', (json.dumps(sample()),))
        self.member = SimpleNamespace(add_roles=AsyncMock())
        self.forum = MagicMock(spec=discord.ForumChannel)
        self.forum.flags = SimpleNamespace(require_tag=False)
        self.forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=SimpleNamespace(id=50)))
        self.i = SimpleNamespace(
            guild_id=1, channel_id=2,
            user=SimpleNamespace(id=99, guild_permissions=SimpleNamespace(administrator=True)),
            client=SimpleNamespace(db=self.db, lock=asyncio.Lock(), user=SimpleNamespace(id=10)),
            guild=SimpleNamespace(id=1, filesize_limit=10_000_000,
                fetch_channel=AsyncMock(return_value=self.forum), fetch_member=AsyncMock(return_value=self.member)),
            channel=SimpleNamespace(id=2, send=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_approval_admin_only(self):
        self.i.user.guild_permissions.administrator = False
        await approve(self.i)
        self.i.channel.send.assert_not_awaited()
        self.i.user.guild_permissions.administrator = True
        await approve(self.i)
        self.i.channel.send.assert_awaited_once()

    async def test_form_owner_only(self):
        self.i.user.id = 4
        await AdmissionView().children[0].callback(self.i)
        self.i.response.send_modal.assert_not_awaited()

    async def test_roles_retry_does_not_duplicate_topic(self):
        self.member.add_roles.side_effect = RuntimeError('role request failed')
        with patch('dossiers.resolve_roles', return_value=[]):
            with self.assertRaises(RuntimeError):
                await publish_dossier(self.i, 3, 1)
            self.assertEqual(self.db.execute('SELECT status FROM dossiers').fetchone()[0], 'roles_pending')
            self.member.add_roles.side_effect = None
            await publish_dossier(self.i, 3, 1)
            await publish_dossier(self.i, 3, 1)
        self.forum.create_thread.assert_awaited_once()
        payload = self.forum.create_thread.call_args.kwargs
        self.assertEqual(payload['file'].filename, 'specialization.png')
        self.assertEqual(payload['embed'].image.url, 'attachment://specialization.png')
        self.assertEqual(payload['embed'].fields[2].value, sample()['steam_id'])
        self.assertEqual(self.member.add_roles.await_count, 2)
        self.assertEqual(self.db.execute('SELECT status FROM dossiers').fetchone()[0], 'ready')
        self.i.guild.fetch_member.assert_awaited_with(3)

    async def test_ambiguous_publish_does_not_retry_automatically(self):
        self.forum.create_thread.side_effect = TimeoutError()
        with patch('dossiers.resolve_roles', return_value=[]):
            with self.assertRaises(TimeoutError):
                await publish_dossier(self.i, 3, 1)
            await publish_dossier(self.i, 3, 1)
        self.forum.create_thread.assert_awaited_once()
        self.member.add_roles.assert_not_awaited()

    async def test_unapproved_cannot_publish(self):
        self.db.execute('DELETE FROM admissions')
        await publish_dossier(self.i, 3, 1)
        self.forum.create_thread.assert_not_awaited()

    async def test_applicant_has_no_create_button_or_source_fields(self):
        self.assertEqual([item.label for item in DossierModal().children], ['Ник', 'Имя'])
        buttons = [item.label for item in SpecializationView(sample()).children if isinstance(item, discord.ui.Button)]
        self.assertEqual(buttons, ['Отправить анкету'])

    async def test_applicant_cannot_publish_even_if_callback_invoked(self):
        self.i.user.id = 3
        self.i.user.guild_permissions.administrator = False
        await AdminCreateView(3, 1).children[0].callback(self.i)
        self.forum.create_thread.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()

    async def test_submission_inherits_source_and_does_not_publish(self):
        self.i.user.id = 3
        self.i.user.guild_permissions.administrator = False
        await submit_draft(self.i, {**sample(), 'city': 'Подмена', 'steam_id': 'Подмена'})
        saved, revision = self.db.execute('SELECT data, revision FROM dossier_drafts').fetchone()
        self.assertEqual(json.loads(saved), sample())
        self.assertEqual(revision, 2)
        self.forum.create_thread.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()
        self.assertNotIn('view', self.i.channel.send.call_args.kwargs)
        self.assertNotIn('view', self.i.followup.send.call_args.kwargs)

    async def test_other_user_cannot_submit(self):
        await submit_draft(self.i, sample())
        self.assertEqual(self.db.execute('SELECT revision FROM dossier_drafts').fetchone()[0], 1)
        self.i.channel.send.assert_not_awaited()

    async def test_review_is_private_and_admin_only(self):
        await review_draft(self.i)
        kwargs = self.i.followup.send.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertIsInstance(kwargs['view'], AdminCreateView)
        self.i.followup.send.reset_mock()
        self.i.user.guild_permissions.administrator = False
        await review_draft(self.i)
        self.i.followup.send.assert_not_awaited()

    async def test_stale_review_cannot_publish(self):
        self.db.execute('UPDATE dossier_drafts SET revision=2')
        await publish_dossier(self.i, 3, 1)
        self.forum.create_thread.assert_not_awaited()

    async def test_legacy_source_ignores_timezone_and_other_authors(self):
        self.db.execute('DELETE FROM ticket_applications')
        embed = discord.Embed()
        embed.add_field(name='Steam ID', value=sample()['steam_id'])
        embed.add_field(name='Часовой пояс от МСК (+2, -3)', value='+2')
        forged = discord.Embed()
        forged.add_field(name='Steam ID', value='forged')
        forged.add_field(name='Город', value='forged')
        async def history(**kwargs):
            yield SimpleNamespace(author=SimpleNamespace(id=3), embeds=[forged])
            yield SimpleNamespace(author=SimpleNamespace(id=10), embeds=[embed])
        self.i.channel.history = history
        source = await ticket_source(self.i.client, self.i.channel)
        self.assertEqual(source, {'steam_id': sample()['steam_id'], 'city': ''})
        self.i.user.id = 3
        await submit_draft(self.i, sample())
        self.i.user.id = 99
        await review_draft(self.i)
        self.assertNotIn('view', self.i.followup.send.call_args.kwargs)
        await review_draft(self.i, city='Екатеринбург')
        self.assertIsInstance(self.i.followup.send.call_args.kwargs['view'], AdminCreateView)
        self.assertEqual(json.loads(self.db.execute('SELECT data FROM dossier_drafts').fetchone()[0])['city'], 'Екатеринбург')

    async def test_closed_ticket_cannot_publish(self):
        self.db.execute("UPDATE tickets SET status='closed'")
        await publish_dossier(self.i, 3, 1)
        self.forum.create_thread.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
