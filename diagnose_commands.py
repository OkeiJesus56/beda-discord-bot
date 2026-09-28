"""Read-only Discord registration check. Never prints credentials."""
import json
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import dotenv_values


def main():
    config = dotenv_values(Path(__file__).with_name('.env'))
    token = config.get('DISCORD_TOKEN')
    guild_id = config.get('DISCORD_GUILD_ID', '')
    if not token or not guild_id.isdigit():
        print('Missing token or valid guild ID in .env')
        return

    def get(path):
        request = urllib.request.Request(
            'https://discord.com/api/v10' + path,
            headers={'Authorization': 'Bot ' + token, 'User-Agent': 'BEDA-Diagnostics/1.0'},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    try:
        app = get('/oauth2/applications/@me')
        guild = get('/guilds/' + guild_id)
        commands = get('/applications/' + app['id'] + '/guilds/' + guild_id + '/commands')
        print(json.dumps({
            'application': app['name'], 'application_id': app['id'],
            'server': guild['name'], 'server_id': guild_id,
            'commands': [{'name': c['name'], 'default_member_permissions': c.get('default_member_permissions')} for c in commands],
        }, ensure_ascii=True, indent=2))
    except urllib.error.HTTPError as error:
        print('Discord HTTP status:', error.code)
    except urllib.error.URLError:
        print('Network connection failed')


if __name__ == '__main__':
    main()
