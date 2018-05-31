# coding=utf8

#==============================================================================#
from __future__ import print_function

from contextlib import closing
from itertools import *
from math import *
from urllib2 import URLError, HTTPError
from ssl import SSLError
import collections
import traceback
import urllib
import urllib2
import socket
import zlib
import ssl
import re
import locale
import os.path
import sys

from bs4 import BeautifulSoup
from untwisted.magic import sign

from url_collect import URL_PART_RE
from util import multi
from runtime import later
import url_collect
import runtime
import util
import imgur
import identity

#==============================================================================#
link, install, uninstall = util.LinkSet().triple()

USER_AGENT = 'Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:60.0) Gecko/20100101 Firefox/60.0'
ACCEPT_ENCODING = 'gzip, deflate'

TIMEOUT_S = 20
READ_BYTES_MAX = 1024*1024
CMDS_PER_LINE_MAX = 6
GIBG_CACHE_SIZE = 128
BS4_PARSER = 'html5lib'

MAX_AURL = 35
MAX_DESC_LEN = 100

CONF_FILE = 'conf/url.py'
conf = util.fdict(CONF_FILE) if os.path.exists(CONF_FILE) else {}

def get_default_headers():
    yield 'User-Agent', USER_AGENT
    yield 'Accept-Encoding', ACCEPT_ENCODING
    language, encoding = locale.getdefaultlocale()
    if language != 'C':
        yield 'Accept-Language', language

default_headers = tuple(get_default_headers())

class CustomHTTPRedirectHandler(urllib2.HTTPRedirectHandler):
    max_repeats = 12
    max_redirections = 30

def get_opener(bind_host=None):
    if bind_host is None and 'bind_host' in conf:
        bind_host = conf['bind_host']
    elif bind_host is None and 'bind_hosts' in conf:
        bind_host = conf['bind_hosts'][0]

    return util.ext_url_opener(
        bind_host   = bind_host,
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23),
        handlers    = (urllib2.HTTPCookieProcessor,
                       CustomHTTPRedirectHandler))

#==============================================================================#
@link('HELP*')
def h_help(bot, reply, args):
    reply('url [URL ...]',
    'Shows the titles of recently mentioned URLs, or of a specific URL.')

@link(('HELP', 'url'), ('HELP', 'title'))
def h_help_url(bot, reply, args):
    reply('url [URL ...]',
    'If URL is given, shows the title of the HTML page or image it locates;'
    ' otherwise, shows the titles of all URLs in the most recent channel'
    ' message which contains a URL, and for which "url" has not already been'
    ' called. Further "!url" commands (up to %s in total) may be given on the'
    ' same line.' % CMDS_PER_LINE_MAX)

@link('!url', '!title', ('ACTION', '!url'), ('ACTION', '!title'))
@multi('!url', '!title', limit=CMDS_PER_LINE_MAX, prefix=False)
def h_url(bot, id, target, args, full_msg, reply):
    channel = (target or ('%s!%s@%s' % id)).lower()

    if args:
        urls = url_collect.extract_urls(args)
        yield sign('URL_CMD_URLS', bot, urls, target, id, full_msg)
    elif url_collect.history[channel]:
        urls = url_collect.history[channel].pop(-1)
    else:
        urls = None

    if not urls:
        reply('No URL found.')
        return

    for url in urls:
        try:
            result = get_title_proxy(url)
            reply(result['title'])

            # Generate a URL-suppressed proxy message for the basic component.
            btitle = result['title_bare']
            p_target = '%s!%s@%s' % id if target is None else target
            if isinstance(btitle, unicode):
                btitle = btitle.encode('utf-8')
            yield later(sign('PROXY_MSG', bot, None, p_target, btitle,
                             no_url=True))

            if result.get('proxy'):
                # Generate a quiet proxy message for the parenthetical component.
                pmsg, fmsg = result['proxy'], result['proxy_full']
                if isinstance(pmsg, unicode): pmsg = pmsg.encode('utf-8')
                if isinstance(fmsg, unicode): fmsg = fmsg.encode('utf-8')
                yield later(sign('PROXY_MSG', bot, None, p_target, pmsg,
                                 full_msg=fmsg, quiet=True, no_url=True))

            yield runtime.sleep(0.01)
        except Exception as e:
            traceback.print_exc()
            url, is_nsfw = url_collect.url_nsfw(url)
            reply('Error: %s [%s%s]' %
                (e, abbrev_url(url), ' \2NSFW\2' if is_nsfw else ''))

#==============================================================================#
# May be raised by get_title, get_title_proxy, etc, to indicate that the
# retrieval of information about a URL failed in some controlled fashion.
class PageURLError(Exception):
    pass

# Returns an IRC string describing the given URL, including its title; suitable
# for sending over IRC as a single line, as in response to the !url command.
def get_title(url):
    return get_title_proxy(url)['title']

# Returns a dictionary containing the following keys:
#   'title':          An IRC string describing the URL, including its title.
#   'title_bare':     'title' without any parenthetical information.
#   'proxy_msg':      The part of 'title' considered to be a proxy message.
#   'proxy_msg_full': The unabbreviated version of 'proxy_msg'.
def get_title_proxy(url):
    url, is_nsfw = url_collect.url_nsfw(url)
    url = utf8_url_to_ascii(url)

    request = urllib2.Request(url)
    for header in default_headers:
        request.add_header(*header)

    host = request.get_host()
    if not is_global_address(host): raise PageURLError(
        'Access to this host is denied: %s.' % host)

    exceptions = []
    for bind_host in conf.get('bind_hosts', [None]):
        try:
            with closing(get_opener(bind_host=bind_host).open(
            request, timeout=TIMEOUT_S)) as stream:
                info = stream.info()
                ctype = info.gettype()
                size = info['Content-Length'] if 'Content-Length' in info else None
                final_url = stream.geturl()
                parts = get_title_parts(
                    final_url, ctype, stream=stream, bind_host=bind_host)
                break
        except (HTTPError, URLError, SSLError, socket.error) as e:
            if isinstance(e, HTTPError):
                if e.code not in (403, 503): raise
            elif isinstance(e, SSLError):
                if e.args != ('The read operation timed out',): raise
            elif isinstance(e, URLError) and isinstance(e.reason, IOError):
                if e.reason.errno != -2: raise
            print('[bind_host=%s] %r, %r, errno=%r' % (bind_host, e,
                type(e), getattr(e, 'errno', None)), file=sys.stderr)
            exceptions.append(e)
    else:
        raise exceptions[0]

    title = parts.get('title', 'Title: (none)')
    extra = parts.get('info', ctype)
    final_url = parts.get('url', final_url)
    size = parts.get('size', size)

    url_info = []
    if final_url != url:
        url_info[:0] = ['%s -> %s' % (abbrev_url(url),
            abbrev_url_middle(final_url))]
    else:
        url_info[:0] = [abbrev_url(url)]

    if extra:
        url_info[:0] = [extra]

    if size:
        url_info[:0] = [bytes_to_human_size(size)]
   
    url_info = '; '.join(url_info)

    is_nsfw |= parts.get('nsfw', False)
    if is_nsfw: url_info = '%s \2NSFW\2' % url_info

    return {
        'title':      '%s [%s]' % (title, url_info),
        'title_bare': title,
        'proxy':      parts.get('proxy'),
        'proxy_full': parts.get('proxy_full') }

#-------------------------------------------------------------------------------
# Given a URL and its MIME type (according to HTTP), and possibly also given a
# file object containing a stream with the contents of the URL, returns a
# dictionary containing some or all of the following keys:
#   'title':      the main title of the URL.
#   'info':       a string with supplementary information about the URL.
#   'url':        the (new) URL to which the original URL ultimately directs.
#   'size':       the size in bytes of the resource given by the 'url' key.
#   'proxy':      the part of 'info' (if any) considered to be a proxy message.
#   'proxy_full': the unabbreviated version (if any) of 'proxy'.
#   'nsfw':       True to indicate that the content is "not safe for work".
def get_title_parts(url, type, **kwds):
    match = URL_PART_RE.match(url)
    path, query = decode_url_path(match.group('path'))
    # YouTube
    if re.search(r'(^|\.)youtube\.com$', match.group('host')):
        res = get_title_youtube(url, type, **kwds)
        if res: return res
    # imgur
    if re.match(r'(www\.|i\.)?imgur\.com$', match.group('host')):
        res = get_title_imgur(url, type, **kwds)
        if res: return res
    # HTML
    if 'html' in type:
        res = get_title_html(url, type, **kwds)
        if res: return res
    # image files
    if type.startswith('image/'):
        return get_title_image(url, type, **kwds)
    # Other
    return dict()

#-------------------------------------------------------------------------------
def get_title_html(url, type, stream=None, **kwds):
    if stream is None:
        request = urllib2.Request(url)
        for header in default_headers:
            request.add_header(*header)
        stream = get_opener().open(
            request, timeout=TIMEOUT_S)

    with closing(stream):
        charset = stream.info().getparam('charset')
        content_enc = stream.info().dict.get('content-encoding', 'identity')
        if content_enc == 'identity':
            data = stream.read(READ_BYTES_MAX)
        elif content_enc == 'gzip':
            raw_data = stream.read(READ_BYTES_MAX)
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw_data)
        elif content_enc == 'deflate':
            raw_data = stream.read(READ_BYTES_MAX)
            try:
                data = zlib.decompressobj().decompress(raw_data)
            except zlib.error:
                data = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw_data)
        else:
            raise PageURLError(
                'Unsupported content-encoding: "%s"' % content_enc)

    soup = BeautifulSoup(data, BS4_PARSER, from_encoding=charset)
    title = soup.find('title')
    if title:
        title = ''.join(re.sub(r'\s+', ' ', s) for s in title.strings).strip()
        return { 'title': 'Title: %s' % format_title(title) }

#-------------------------------------------------------------------------------
def get_title_image(url, type, **kwds):
    title = google_image_best_guess(url, **kwds)
    title = 'Best guess: %s' % (format_title(title) if title else '(none)')
    return { 'title': title }

#-------------------------------------------------------------------------------
def get_title_youtube(url, type, **kwds):
    match = URL_PART_RE.match(url)
    path, query = decode_url_path(match.group('path'))
    if path != '/watch' or not query.get('v'): return
    video_id = query['v']
    try:
        from youtube import youtube
        result = youtube.videos().list(id=video_id,
            part='snippet,contentDetails').execute()['items'][0]

        title = result['snippet']['title']
        desc_full = result['snippet']['description']
        desc = format_description(desc_full)
        channel = result['snippet']['channelId']
        channel = result['snippet'].get('channelTitle', channel)
        duration = result['contentDetails']['duration']
        duration = iso8601_period_human(duration)

        url_info = {
            'title':        'Title: %s' % format_title(title),
            'info':         'Duration: %s; Channel: %s; Description: "%s"'
                            % (duration, channel, desc),
            'proxy':        'Description: "%s"' % desc,
            'proxy_full':   'Description: "%s"' % desc_full}

        rating = result['contentDetails'].get('contentRating')
        if rating and rating.get('ytRating') == 'ytAgeRestricted':
            url_info['nsfw'] = True

        return url_info

    except Exception as e:
        traceback.print_exc(e)

#-------------------------------------------------------------------------------
def get_title_imgur(url, type, stream=None, **kwds):
    match = URL_PART_RE.match(url)
    path, query = decode_url_path(match.group('path'))
    path_match = re.match(
        r'(?P<section>(/a|/gallery|/r/[^/]+)?)/'
        r'(?P<id>[a-zA-Z0-9]+)(\.[a-zA-Z0-9]+)?$', path)
    if not path_match: return
    section, id = path_match.group('section', 'id')
   
    try:
        if section == '/a':
            imgur_info = imgur.album_info(id)
        elif section == '/gallery' or section.startswith('/r/'):
            imgur_info = imgur.gallery_info(id)
        else:
            imgur_info = imgur.image_info(id)
    except (imgur.ImgurError, urllib2.HTTPError):
        return

    url_info = {}
    if imgur_info.get('title') and not URL_PART_RE.match(imgur_info['title']):
        url_info['title'] = 'Title: %s' % format_title(imgur_info.get('title'))

    if 'images' not in imgur_info:
        add_imgur_image_info(imgur_info, url_info, path, type)
    elif len(imgur_info['images']) == 1:
        imgur_image_info = imgur_info['images'][0]
        url_info = add_imgur_image_info(imgur_image_info, url_info, path, type)
    else:
        if any(i.get('nsfw') for i in imgur_info['images']):
            url_info['nsfw'] = True
        elif all(not i.get('nsfw') for i in imgur_info['images']):
            url_info['nsfw'] = False
        url_info['info'] = '%d images' % len(imgur_info['images'])
        url_info['size'] = None

    url_info = add_imgur_general_info(imgur_info, url_info)
    return url_info

def add_imgur_image_info(imgur_info, url_info, path, orig_type):
    img_url = imgur_info['link']
    img_type = imgur_info['type']
    type = img_type if orig_type == 'text/html' else orig_type

    title = get_title_image(img_url, img_type)['title']
    if url_info.get('title'):
        title = url_info['title'] + ' -- ' + title

    if orig_type not in ('image/gif', 'video/mp4') \
    and imgur_info.get('gifv'):
        img_url = imgur_info['gifv']
        type, url_info['size'] = None, None

    if orig_type == 'text/html' and not path.endswith('.gifv') \
    and decode_url_path(URL_PART_RE.match(img_url).group('path'))[0] != path:
        url_info['url'] = img_url

    if imgur_info.get('nsfw') is not None:
        url_info['nsfw'] = imgur_info['nsfw']

    url_info['info'] = type
    url_info['title'] = title
    return url_info

def add_imgur_general_info(imgur_info, url_info):
    info_parts = [url_info['info']] if url_info.get('info') else []

    if imgur_info.get('account_url'):
        info_parts.append('Account: %s' % imgur_info['account_url'])

    if imgur_info.get('description'):
        desc_full = imgur_info['description']
        desc = format_description(desc_full)
        url_info['proxy'] = 'Description: "%s"' % desc
        url_info['proxy_full'] = 'Description: "%s"' % desc_full
        info_parts.append(url_info['proxy'])

    if imgur_info.get('section'):
        info_parts.append('Section: "%s"' % imgur_info['section'])

    url_info['info'] = '; '.join(info_parts) if info_parts else None
    return url_info    

#-------------------------------------------------------------------------------
ABBREV_URL_RE = re.compile(
    r'(?P<site>.+?://[^/]*)'
    r'(?P<path>/?.*)')

def abbrev_url(url):
    url = url_to_unicode(url)
    if len(url) > MAX_AURL:
        url = ABBREV_URL_RE.match(url).group('path')
        return '...' + url[-(MAX_AURL-3):]
    else:
        return url

def abbrev_url_middle(url):
    url = url_to_unicode(url)
    site, path = ABBREV_URL_RE.match(url).group('site', 'path')
    return site + '/...' + \
           path[1:][min(-MAX_AURL/2, len(path)-1-(MAX_AURL-3)):] \
           if len(url) > MAX_AURL else url

def url_to_unicode(url):
    if type(url) == unicode: url = url.encode('utf8')
    url = ascii_url_to_utf8(url)
    try: return url.decode('utf8')
    except UnicodeError: return url

def format_title(title):
    title = re.sub(r'\r\n|\r|\n', ' ', title)
    if len(title) > 300:
        return '\2%s\2(...)' % title[:300]
    else:
        return '\2%s\2' % title

def bytes_to_human_size(bytes):
    bytes = int(bytes)
    for (m,s) in (1,'B'),(2**10,'KiB'),(2**20,'MiB'),(2**30,'GiB'):
        units = bytes / m
        if units >= 1024: continue
        return ('%.1f %s' if m>1 else '%d %s') % (units, s)

#===============================================================================
# Returns the "best guess" phrase that Google's reverse image search offers to
# describe the image at the given URL, or None if no such phrase is offered.
gibg_cache = dict()
def google_image_best_guess(url, use_cache=False, **kwds):
    if use_cache and url in gibg_cache:
        return gibg_cache[url]

    PHRASE = 'Best guess for this image:'
    soup = google_image_title_soup(url, **kwds)
    node = soup.find(text=re.compile(re.escape(PHRASE)))

    result = node and node.parent.text.replace(PHRASE, '').strip()
    if use_cache:
        gibg_cache[url] = result
        while len(gibg_cache) > GIBG_CACHE_SIZE:
            gibg_cache.popitem()
    return result

def google_image_title_soup(url, bind_host=None, **kwds):
    request = urllib2.Request('https://www.google.com/searchbyimage?'
        + urllib.urlencode({'image_url':url, 'safe':'off'}))
    request.add_header('Referer', 'https://www.google.com/imghp?hl=en&tab=wi')
    request.add_header('User-Agent', USER_AGENT)

    opener = get_opener(bind_host=bind_host)
    with closing(opener.open(request, timeout=TIMEOUT_S)) as stream:
        text = stream.read(READ_BYTES_MAX)
        return BeautifulSoup(text, BS4_PARSER)

#==============================================================================#
# True if the given hostname or IPV4 or IPV6 address string is not in any
# address range reserved for private or local use, or otherwise False.
def is_global_address(host):
    # See: http://en.wikipedia.org/wiki/Reserved_IP_addresses
    family, _, _, _, address = socket.getaddrinfo(host, None)[0]
    if family == socket.AF_INET:
        host, _ = address
        addr = inet4_int(host)
        for range in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
        '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
        '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
        '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4', '255.255.255.255/32'):
            prefix, size = range.split('/')
            prefix, size = inet4_int(prefix), int(size)
            if addr>>(32-size) == prefix>>(32-size): return False
        return True
    elif family == socket.AF_INET6:
        host, _, _, _ = address
        addr = inet6_int(host)
        for range in ('::/128', '::1/128', '::ffff:0:0/96', '64:ff9b::/96',
        '2001::/32', '2001:10::/28', '2001:db8::/32', '2002::/16', 'fc00::/7',
        'fe80::/10', 'ff00::/8'):
            prefix, size = range.split('/')
            prefix, size = inet6_int(prefix), int(size)
            if addr>>(128-size) == prefix>>(128-size): return False
        return True
    else:
        raise PageURLError(
            'Unsupported address family for "%s": %s.' % (host, family))    

#==============================================================================#
# IPV4 address string to integer.
def inet4_int(addr):
    addr = inet4_tuple(addr)
    return sum(addr[-i-1]<<(8*i) for i in xrange(4))

# IPV6 address string to integer.
def inet6_int(addr):
    addr = inet6_tuple(addr)
    return sum(addr[-i-1]<<(16*i) for i in xrange(8))

# IPV4 address string to 4-tuple of integers.
def inet4_tuple(addr):
    return tuple(int(part) for part in addr.split('.'))

# IPV6 address string to 8-tuple of integers, allowing :: notation.
def inet6_tuple(addr):
    addr = addr.split('::', 1)
    if len(addr) > 1:
        addr0, addr1 = inet6_tuple_base(addr[0]), inet6_tuple_base(addr[1])
        return addr0 + (0,)*(8 - len(addr0) - len(addr1)) + addr1
    else:
        return inet6_tuple_base(addr[0])

# As inet6_tuple(), but does not allow :: notation.
def inet6_tuple_base(addr):
    return tuple(int(part, 16) for part in addr.split(':')) if addr else ()

#==============================================================================#
def utf8_url_to_ascii(url):
    m = URL_PART_RE.match(url)
    if not m: return url
    return m.group('pref') \
         + utf8_host_to_ascii(m.group('host')) \
         + m.group('suff') \
         + utf8_percent_encode(m.group('path') + m.group('frag'))

def utf8_host_to_ascii(host):
    parts = host.split('.')
    for i in xrange(len(parts)):
        try: d_part = parts[i].decode('utf8')
        except UnicodeError: continue
        if d_part.encode('ascii', 'ignore') != d_part:
            parts[i] = 'xn--' + d_part.lower().encode('punycode')
    return '.'.join(parts)

def utf8_percent_encode(url_part):
    return ''.join(
        c if ord(c) in range(128) else ('%%%02X' % ord(c)) for c in url_part)

#-------------------------------------------------------------------------------
def ascii_url_to_utf8(url):
    m = URL_PART_RE.match(url)
    if not m: return url
    return m.group('pref') \
         + ascii_host_to_utf8(m.group('host')) \
         + m.group('suff') \
         + utf8_percent_decode(m.group('path') + m.group('frag'))

def ascii_host_to_utf8(host):
    parts = host.split('.')
    for i in xrange(len(parts)):
        if not parts[i].startswith('xn--'): continue
        try: parts[i] = parts[i][4:].decode('punycode').encode('utf8')
        except UnicodeError: pass
    return '.'.join(parts)

def utf8_percent_decode(url_part):
    def repl(match):
        code = int(match.group('code'), 16)
        return match.group() if code in xrange(128) else chr(code)
    return re.sub(r'%(?P<code>[A-Fa-f0-9]{2})', repl, url_part)

#-------------------------------------------------------------------------------
# Returns (p, q), where p is the given URL path excluding any query part, and
# q is a (possibly empty) dict mapping query names to (possibly null) values.
def decode_url_path(url_path):
    match = re.match(r'(?P<p>[^?]*)(\?(?P<q>.*))?', url_path)
    data = dict()
    if match.group('q'):
        for item in re.split(r'&|;', match.group('q')):
            item = item.split('=', 1)
            key = urllib.unquote_plus(item[0])
            val = urllib.unquote_plus(item[1]) if len(item) > 1 else None
            data[key] = val
    return (match.group('p'), data)

#-------------------------------------------------------------------------------
# Converts an ISO-8601 time period to a human-readable format.
def iso8601_period_human(spec):
    sec, hou, min, day, mon, yea = 0, 0, 0, 0, 0, 0
    match = re.match(r'P(?P<d>.*?)(T(?P<t>.*))?$', spec)
    if not match: raise Exception('Invalid ISO-8601 time period: %s.' % spec)
    for m in re.finditer(r'(?P<n>\d+)(?P<t>\D)', match.group('d')):
        type, n = m.group('t'), int(m.group('n'))
        if   type == 'Y': yea += n
        elif type == 'M': mon += n
        elif type == 'W': day += n*7
        elif type == 'D': day += n
        else: raise Exception('Unknown ISO-8601 date unit: %s.' % type)
    for m in re.finditer(r'(?P<n>\d+)(?P<t>\D)', match.group('t') or ''):
        type, n = m.group('t'), int(m.group('n'))
        if   type == 'H': hou += n
        elif type == 'M': min += n
        elif type == 'S': sec += n
        else: raise Exception('Unknown ISO-8601 time unit: %s.' % type)
    return ''.join((
        '%d year%s, '  % (yea, 's' if yea>1 else '') if yea else '',
        '%d month%s, ' % (mon, 's' if mon>1 else '') if mon else '',
        '%d day%s, '   % (day, 's' if day>1 else '') if day else '',
        '%02d:' % hou if hou else '', '%02d:%02d' % (min, sec)))

def format_description(desc_full):
    desc = re.sub(r'\r\n|\r|\n', ' ', desc_full)
    desc = '%s' % desc[:MAX_DESC_LEN]
    full_urls = url_collect.extract_urls(desc_full)
    for desc_url in url_collect.extract_urls(desc):
        if (desc_url not in full_urls and desc.endswith(desc_url)
        and not ABBREV_URL_RE.match(desc_url).group('path')):
            desc = desc[:-len(desc_url)]
            break
    desc = desc + '...' if len(desc) < len(desc_full) else desc
    return desc
