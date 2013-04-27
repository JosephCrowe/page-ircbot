# coding=utf8

from __future__ import print_function

import socket

from untwisted.mode import Mode
from untwisted.network import Work
from untwisted.event import DATA, BUFFER, FOUND, CLOSE, RECV_ERR
from untwisted.utils import std
from untwisted.utils.common import append, shrug

import re
import sys
import util
import debug
import runtime
from control import NotInstalled, AlreadyInstalled


RECONNECT_DELAY_SECONDS = 1

conf_servers = util.table('conf/mc_servers.py', 'server', socket.__dict__)
conf_channels = util.read_list('conf/mc_channels.py')

channels = map(lambda l: map(str.lower, l), conf_channels)

mc_work = []
mc_mode = Mode()
mc_mode.domain = 'mc'
mc_link = util.LinkSet()
mc_link.link_module(std)
mc_link.link(DATA, append)
mc_link.link(BUFFER, shrug, '\n')
if '--debug' in sys.argv: mc_link.link_module(debug)

ab_mode = None
ab_link = util.LinkSet()


def init_work(server):
    sock = socket.socket(server.family, socket.SOCK_STREAM)
    work = Work(mc_mode, sock)
    mc_work.append(work)
    work.minecraft = server
    work.connect_ex(server.address)

def kill_work(work):
    work.destroy()
    work.shutdown(socket.SHUT_RDWR)
    work.close()
    mc_work.remove(work)

def install(bot):
    global ab_mode
    if ab_mode is not None: raise AlreadyInstalled

    ab_mode = bot
    ab_link.install(ab_mode)

    mc_link.install(mc_mode)
    names = set(s for c in channels for s in c)
    for server in conf_servers:
        if server.name.lower() not in names: continue
        init_work(server)

def uninstall(bot):
    global ab_mode
    if ab_mode is None: raise NotInstalled

    mc_link.uninstall(mc_mode)
    while len(mc_work):
        kill_work(mc_work[0])

    ab_link.uninstall(ab_mode)
    ab_mode = None


@mc_link('SERVER_MSG')
@mc_link('CHANNEL_MSG')
def mc_msg(source, msg):
    if msg.startswith('!'): return
    msg = '%s: %s' % (source, msg)
    for group in channels:
        if source.lower() not in group: continue
        notify_group(group, msg, source)


def notify_group(group, msg, source):
    for target in group:
        if target.lower() == source.lower(): continue
        if target.startswith('#'):
            notify_channel(target, msg, source)
        else:
            notify_server(target, msg, source)

def notify_channel(channel, msg, source):
    def escape(match):
        return {
#            '0':'\x031',  '1':'\x032',  '2':'\x033',  '3':'\x0310',
#            '4':'\x035',  '5':'\x036',  '6':'\x038',  '7':'\x0315',
#            '8':'\x0314', '9':'\x0312', 'a':'\x039',  'b':'\x0311',
#            'c':'\x034',  'd':'\x0313', 'e':'\x038',  'f':'\x0316',
            'l':'\x02',     # bold
            'n':'\x1f',     # underline
#            'o':'\x1d',     # italic
            'r':'\x0f'      # regular
        }.get(match.group(1), '')
    if not source.startswith('#'):
        msg = re.sub(r'§(.?)', escape, msg)
    ab_mode.send_msg(channel, msg)

def notify_server(server, msg, source):
    if source.startswith('#'):
        msg = msg.replace('§', 'S')
    else:
        msg = re.sub(r'§.?', '', msg)
    msg = re.sub(r'[\x00-\x1f]', '', msg)
    for work in mc_work:
        if work.minecraft.name.lower() != server.lower(): continue
        work.dump(msg + '\n')
        break


@mc_link(FOUND)
def mc_found(work, line):
    if line.startswith('<%s>' % work.minecraft.agent): return
    yield util.msign(mc_mode, 'SERVER_MSG', work.minecraft.name, line)

@mc_link(CLOSE)
@mc_link(RECV_ERR)
def mc_close_recv_error(work, *args):
    kill_work(work)
    yield runtime.sleep(RECONNECT_DELAY_SECONDS)
    init_work(work.minecraft)


@ab_link('MESSAGE')
def ab_message(bot, id, chan, msg):
    if msg.startswith('!'): return
    match = re.match(r'\x01ACTION (?P<msg>.*)', msg)
    if match:
        cmsg = '* %s %s' % (id.nick, match.group('msg'))
    else:
        cmsg = '<%s> %s' % (id.nick, msg)
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('OTHER_JOIN')
def ab_other_join(bot, id, chan):
    cmsg = '%s joined the channel.' % id.nick
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('OTHER_PART')
def ab_other_part(bot, id, chan, msg):
    cmsg = '%s left the channel.' % id.nick \
        + ((' (%s)' % msg) if msg else '') + '.'
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('OTHER_KICKED')
def ab_other_kick(bot, other_nick, op_id, chan, msg):
    cmsg = '%s was kicked by %s.' % (other_nick, op_id.nick) \
        + ((' (%s)' % msg) if msg else '') + '.'
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('OTHER_QUIT_CHAN')
def ab_other_quit(bot, id, chan, msg):
    cmsg = '%s quit the network.' % id.nick \
        + ((' (%s)' % msg) if msg else '') + '.'
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('OTHER_NICK_CHAN')
def ab_other_nick(bot, id, chan, new_nick):
    cmsg = '%s is now known as %s.' % (id.nick, new_nick)
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('SELF_JOIN')
def ab_self_join(bot, chan):
    cmsg = 'Joined the channel.'
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('SELF_PART')
def ab_self_part(bot, chan, msg):
    cmsg = 'Left the channel%s.' % ((' (%s)' % msg) if msg else '')
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('SELF_KICKED')
def ab_self_kicked(bot, chan, op_id, msg):
    cmsg = 'Kicked from the channel by %s%s.' % \
        (op_id.nick, (' (%s)' % msg) if msg else '')
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)

@ab_link('CLOSING_CHAN')
def ab_self_quit(bot, chan):
    cmsg = 'Disconnected from the network.'
    yield util.msign(mc_mode, 'CHANNEL_MSG', chan, cmsg)
