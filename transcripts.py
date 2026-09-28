"""Export a Discord channel as an HTML transcript and attachment ZIP."""
import html
import json
import re
import tempfile
import zipfile
from pathlib import Path

import discord


def attachment_name(attachment):
    safe = re.sub(r'[^\w.\-]', '_', attachment.filename)[:120].strip('.') or 'file'
    return f'{attachment.id}-{safe}'


def message_html(message):
    escape = lambda value: html.escape(str(value))
    body = [f'<article><h2>{escape(message.author)} · {escape(message.created_at.isoformat())}</h2>',
            f'<small>Автор: {message.author.id}; сообщение: {message.id}</small>',
            f'<pre>{escape(message.content)}</pre>']
    if message.edited_at:
        body.append(f'<p>Изменено: {escape(message.edited_at.isoformat())}</p>')
    if message.reference:
        body.append(f'<p>Ответ на сообщение: {message.reference.message_id}</p>')
    for embed in message.embeds:
        body.append(f'<h3>{escape(embed.title or "")}</h3><pre>{escape(embed.description or "")}</pre>')
        for field in embed.fields:
            body.append(f'<b>{escape(field.name)}</b><pre>{escape(field.value)}</pre>')
        body.append(f'<details><summary>Данные вложенной карточки</summary><pre>{escape(json.dumps(embed.to_dict(), ensure_ascii=False, indent=2))}</pre></details>')
    for attachment in message.attachments:
        body.append(f'<p>Файл: <a href="attachments/{escape(attachment_name(attachment))}">{escape(attachment.filename)}</a></p>')
    for sticker in message.stickers:
        body.append(f'<p>Стикер: {escape(sticker.name)} ({sticker.id})</p>')
    body.append('</article>')
    return '\n'.join(body)


async def export_ticket(channel, destination, owner_id, actor):
    # Temporary files are removed on both success and failure. No transcript database.
    with tempfile.TemporaryDirectory(prefix='beda-transcript-') as directory:
        root = Path(directory)
        archive = root / f'ticket-{channel.id}.zip'
        page = root / 'transcript.html'
        with page.open('w', encoding='utf-8') as output, zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            output.write('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Заявка BEDA</title><style>body{max-width:1000px;margin:30px auto;font-family:Arial}article{border-bottom:1px solid #ccc;padding:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><body>')
            output.write(f'<h1>{html.escape(channel.name)}</h1><p>Заявитель: {owner_id}. Архивировал: {html.escape(str(actor))} ({actor.id}). Время в UTC.</p>')
            async for message in channel.history(limit=None, oldest_first=True):
                output.write(message_html(message))
                for attachment in message.attachments:
                    if attachment.size > channel.guild.filesize_limit:
                        raise ValueError('Вложение превышает лимит Discord. Канал сохранён; архивируйте крупный файл вручную.')
                    path = root / str(attachment.id)
                    await attachment.save(path)
                    bundle.write(path, f'attachments/{attachment_name(attachment)}')
                    path.unlink()
                if archive.stat().st_size > channel.guild.filesize_limit:
                    raise ValueError('Архив превышает лимит загрузки Discord. Канал не удалён.')
            output.write('</body></html>')
            output.flush()
            bundle.write(page, 'transcript.html')
        if archive.stat().st_size > channel.guild.filesize_limit:
            raise ValueError('Архив превышает лимит загрузки Discord. Канал не удалён.')
        file = discord.File(archive)
        try:
            return await destination.send(
                f'Архив заявки **{discord.utils.escape_markdown(channel.name)}**\nЗаявитель: <@{owner_id}> (ID {owner_id})\nАрхивировал: {actor} (ID {actor.id})\nВ ZIP: transcript.html и вложения. Скачайте и распакуйте ZIP для просмотра.',
                file=file, allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            file.close()
