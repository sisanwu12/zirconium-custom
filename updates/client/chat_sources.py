"""Check official DEBs and APT in the existing chat Distrobox; install only on request."""
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import urllib.parse
import urllib.request


CONTAINER = 'chat'
PACKAGES = {'linuxqq': 'QQ', 'wechat': '微信', 'bytedance-feishu-stable': '飞书', 'wemeet': '腾讯会议'}
HEADERS = {'User-Agent': 'Mozilla/5.0 local-chat-updater', 'Cache-Control': 'no-cache'}
APT_OPTIONS = ['-o', 'APT::Update::Error-Mode=any', '-o', 'Acquire::Retries=1',
               '-o', 'Acquire::http::Timeout=20', '-o', 'Acquire::https::Timeout=20']


def command(args, capture=True, timeout=180):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip()[-2000:]
        raise RuntimeError(detail or f'Command failed: {args[0]} ({result.returncode})')
    return result.stdout.strip() if capture else ''


def in_chat(*args, capture=True, timeout=180):
    return command(['podman', 'exec', '--user', '0', '--env', 'LC_ALL=C',
                    '--env', 'DEBIAN_FRONTEND=noninteractive', CONTAINER, *args], capture, timeout)


def ensure_container():
    running = command(['podman', 'inspect', '--format', '{{.State.Running}}', CONTAINER])
    if running != 'true':
        print('chat: starting the existing container to check packages', flush=True)
        command(['podman', 'start', CONTAINER])
    if in_chat('dpkg', '--print-architecture') != 'amd64':
        raise RuntimeError('chat must use amd64 DEB packages')


def installed_packages():
    rows = [line.split('\t') for line in in_chat(
        'dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n').splitlines()]
    return {name: version for name, version, status in rows if status.startswith('ii')}


def is_newer(installed, latest):
    result = subprocess.run(['podman', 'exec', '--user', '0', CONTAINER,
                             'dpkg', '--compare-versions', installed, 'lt', latest],
                            capture_output=True, text=True, timeout=20)
    if result.returncode not in (0, 1):
        raise ValueError('Invalid DEB version: ' + result.stderr.strip())
    return result.returncode == 0


def fetch(url, limit=2 * 1024 * 1024):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Official downloads must use HTTPS')
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Official metadata exceeds size limit')
    return data


def vendor_source(package):
    if package == 'linuxqq':
        script = fetch('https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/linuxConfig.js').decode()
        match = re.search(r'\bvar\s+params\s*=\s*', script)
        if not match:
            raise ValueError('QQ official download configuration changed')
        data, _end = json.JSONDecoder().raw_decode(script[match.end():])
        return {'url': data['x64DownloadUrl']['deb']}
    if package == 'wechat':
        return {'url': 'https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb'}
    if package == 'bytedance-feishu-stable':
        data = json.loads(fetch('https://www.feishu.cn/api/package_info?platform=10'))
        if data.get('code') != 0:
            raise ValueError('Feishu official API failed')
        return {'url': data['data']['download_link'], 'md5': data['data'].get('hash')}
    if package == 'wemeet':
        query = [{'package-type': 'app', 'channel': '0300000000', 'platform': 'linux',
                  'arch': 'x86_64', 'decorators': ['deb']}]
        url = 'https://meeting.tencent.com/web-service/query-download-info?' + urllib.parse.urlencode(
            {'q': json.dumps(query, separators=(',', ':')), 'nonce': secrets.token_hex(8)})
        data = json.loads(fetch(url))
        if data.get('code') != 0 or len(data.get('info-list', [])) != 1:
            raise ValueError('Tencent Meeting official API failed')
        item = data['info-list'][0]
        return {'url': item['url'], 'md5': item.get('md5')}
    raise ValueError('Unknown chat application')


def read_deb_control(stream):
    """Read only the ar headers and control member, never the application's data archive."""
    limit = 1024 * 1024
    consumed = 0

    def read_exact(size):
        nonlocal consumed
        if size < 0:
            raise ValueError('Negative DEB member size')
        consumed += size
        if consumed > limit:
            raise ValueError('DEB control metadata exceeds 1 MiB')
        data = stream.read(size)
        if len(data) != size:
            raise ValueError('Truncated DEB metadata')
        return data

    if read_exact(8) != b'!<arch>\n':
        raise ValueError('Download is not a DEB archive')
    for _ in range(8):
        header = read_exact(60)
        if header[58:60] != b'`\n':
            raise ValueError('Invalid DEB ar header')
        name = header[:16].decode('ascii').strip().rstrip('/')
        size = int(header[48:58].decode('ascii').strip())
        if name.startswith('data.tar'):
            raise ValueError('DEB has no control member before its data')
        payload = read_exact(size)
        if name.startswith('control.tar'):
            with tarfile.open(fileobj=io.BytesIO(payload), mode='r:*') as archive:
                members = [member for member in archive.getmembers()
                           if member.name in ('control', './control') and member.isfile()]
                if len(members) != 1:
                    raise ValueError('DEB must contain exactly one control file')
                with archive.extractfile(members[0]) as control:
                    text = control.read(limit + 1).decode('utf-8')
            if len(text) > limit:
                raise ValueError('Oversized DEB control file')
            fields = dict(line.split(': ', 1) for line in text.splitlines()
                          if line and not line[0].isspace() and ': ' in line)
            return {key: fields[key] for key in ('Package', 'Version', 'Architecture')}
        if size % 2:
            read_exact(1)
    raise ValueError('DEB control member not found')


def official_release(package):
    source = vendor_source(package)
    url = source['url']
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Official download must use HTTPS')
    headers = {**HEADERS, 'Range': 'bytes=0-1048575'}
    # Even when a server ignores Range, consume only the archive's initial metadata.
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        fields = read_deb_control(response)
        resolved = response.geturl()
    if fields['Package'] != package or fields['Architecture'] != 'amd64':
        raise ValueError('Official DEB has an unexpected package name or architecture')
    if urllib.parse.urlsplit(resolved).scheme != 'https':
        raise ValueError('Official download redirected away from HTTPS')
    return {**source, **fields, 'url': resolved}


def refresh_apt():
    in_chat('apt-get', *APT_OPTIONS, 'update', timeout=180)


def apt_updates():
    output = in_chat('apt-get', '-s', '--with-new-pkgs', 'upgrade')
    return [(name, old or '未安装', new)
            for name, old, new in re.findall(r'^Inst (\S+) (?:\[([^]]+)\] )?\((\S+)', output, re.MULTILINE)]


def validate_download(path, package, version, expected_md5=None):
    with open(path, 'rb') as stream:
        control = read_deb_control(stream)
    if control != {'Package': package, 'Version': version, 'Architecture': 'amd64'}:
        raise ValueError('Downloaded DEB changed since checking; retry instead of installing it')
    if expected_md5:
        if not re.fullmatch(r'[0-9a-fA-F]{32}', expected_md5):
            raise ValueError('Invalid published download checksum')
        digest = hashlib.md5()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest().lower() != expected_md5.lower():
            raise ValueError('Published download checksum mismatch')

