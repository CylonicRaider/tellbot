#!/usr/bin/env python3
# -*- coding: ascii -*-

import sys, os, re, time
import operator, collections
import base64
import fnmatch
import threading
import subprocess
import sqlite3

from xml.sax.saxutils import escape

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

import basebot

INBOX_CUTOFF = 172800 # 2 days
REPLY_TIMEOUT = 172800 # 2 days
GC_INTERVAL = 3600 # 1 hour
NOTBOT_DELAY = 10 # 10 secs
MAIL_SEEN_COOLOFF = 604800 # 1 week
MAIL_SEND_COOLOFF = 604800 # 1 week

HELP_TEXT = '''
To add a message to other users' mailbox, use
    !tell @user1 [@user2 ...] [*group1 ...] message
To create or grow a group of users, use
    !tgroup *group @user1 [@user2 ...] [*group1 ...]
To shrink a group, use
    !tgroup *group -@user1 [-@user2 ...] [-*group1 ...]
To list the members of a group, use
    !tgrouplist *group
For a thorough manual, see https://github.com/CylonicRaider/tellbot/\
blob/master/USAGE.md.
'''[1:-1]

REPLY_HELP = ('Reply with a !reply to any single message to reply to the '
    'author, or with a !reply-all to reply to the group the message was '
    'sent to (or the sender if none).')
USERSPEC_HELP = ('Nicknames must be preceded by an @ sign and may not '
    'contain spaces.')

EMAIL_NOTIFICATION_TEMPLATE = '''
From: %(from)s
To: %(to)s
Subject: %(subject)s
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="%(boundary)s"

This is a multi-part MIME message.

--%(boundary)s
Content-Type: text/plain; charset=utf-8

You have a new unread TellBot message (%(unread_total)s total).

From: %(plain_from)s
To: %(plain_to)s
Priority: %(plain_prio)s
Text: %(plain_text)s

Reply to this email to unsubscribe.

--%(boundary)s
Content-Type: text/html; charset=utf-8

<!DOCTYPE html>
<html>
  <body>
    <p>You have a new unread TellBot message (%(unread_total)s \
total).</p>
    <p><table border=0 cellpadding=0 cellspacing=0>
      <tr><th align=left>From:&nbsp;</th><td>%(html_from)s</td></tr>
      <tr><th align=left>To:&nbsp;</th><td>%(html_to)s</td></tr>
      <tr><th align=left>Priority:&nbsp;</th><td>%(html_prio)s</td></tr>
      <tr><th align=left>Text:&nbsp;</th><td>%(html_text)s</td></tr>
    </table></p>
    <p><small>Reply to this email to unsubscribe.</small></p>
  </body>
</html>

--%(boundary)s--
'''[1:]

def is_true(s):
    if isinstance(s, str):
        return s.lower() in ('yes', 'true', 'on', 'y', '1')
    else:
        return bool(s)

def seminormalize_nick(nick):
    return re.sub(r'\s+', '', nick)
def make_mention(nick):
    return '@' + re.sub(r'\s+', '', nick)

def titlefirst(s):
    if not s: return ''
    return s[0].upper() + s[1:]

def format_list(l, fallback=None):
    l = tuple(l)
    if len(l) == 0:
        return fallback
    elif len(l) <= 2:
        return ' and '.join(l)
    else:
        return ', '.join(l[:-1]) + ', and ' + l[-1]

class OrderedSet:
    @staticmethod
    def deduplicate(inpt, key=lambda x: x, map=lambda x: x):
        seen = set()
        for item in inpt:
            k = key(item)
            if k not in seen:
                yield map(item)
                seen.add(k)

    @classmethod
    def firstel(cls, base=()):
        return cls(base, operator.itemgetter(0))

    def __init__(self, base=(), key=lambda x: x):
        self.list = []
        self.set = set()
        self.key = key
        self.extend(base)

    def __bool__(self):
        return bool(self.list)
    def __nonzero__(self):
        return bool(self.list)

    def __len__(self):
        return len(self.list)

    def __contains__(self, item):
        return self.key(item) in self.set

    def __iter__(self):
        return iter(self.list)

    def __eq__(self, other):
        if not isinstance(other, OrderedSet): return NotImplemented
        return self.set == other.set

    def copy(self):
        return self.__class__(self, key=self.key)

    def clear(self):
        self.list[:] = ()
        self.set.clear()

    def append(self, item):
        key = self.key(item)
        if key not in self.set:
            self.set.add(key)
            self.list.append(item)

    def extend(self, items):
        for item in items:
            self.append(item)

    def discard(self, item):
        key = self.key(item)
        if key in self.set:
            self.set.remove(key)
            self.list[:] = [i for i in self.list if self.key(i) != key]

    def discard_all(self, items):
        for item in items:
            self.discard(item)

    def sort(self, key=None, reverse=False):
        if key is None: key = self.key
        self.list.sort(key=key, reverse=reverse)

class DBLock:
    class Committer:
        def __init__(self, parent):
            self.parent = parent

        def __enter__(self):
            self.parent.acquire(commit=True)

        def __exit__(self, t, v, tb):
            self.parent.release()

    def __init__(self, conn=None):
        self.lock = threading.RLock()
        self.conn = conn
        self.commit = False
        self.counter = 0
        self.committing = self.Committer(self)

    def __enter__(self):
        self.acquire()

    def __exit__(self, t, v, tb):
        self.release()

    def acquire(self, blocking=True, commit=False):
        ret = self.lock.acquire(blocking)
        if commit: self.commit = True
        self.counter += 1
        return ret

    def release(self):
        if not self.lock._is_owned():
            raise RuntimeError('Trying to release foreign lock!')
        self.counter -= 1
        if self.counter == 0 and self.commit:
            if self.conn: self.conn.commit()
            self.commit = False
        return self.lock.release()

class NotificationDistributor:
    def __enter__(self):
        raise NotImplementedError
    def __exit__(self, t, v, tb):
        raise NotImplementedError
    def normalize_user(self, name):
        return (basebot.normalize_nick(name), seminormalize_nick(name))
    def query_user(self, name):
        raise NotImplementedError
    def query_aliases(self, base):
        raise NotImplementedError
    def update_aliases(self, base, names):
        raise NotImplementedError
    def query_seen(self, user):
        raise NotImplementedError
    def update_seen(self, user, name, time, unread, room):
        raise NotImplementedError
    def list_groups(self):
        raise NotImplementedError
    def query_groups_of(self, user):
        raise NotImplementedError
    def query_group(self, name, raw=False):
        raise NotImplementedError
    def update_group(self, name, members):
        raise NotImplementedError
    def query_groupdesc(self, name):
        raise NotImplementedError
    def update_groupdesc(self, name, description):
        raise NotImplementedError
    def message_bounds(self, user):
        raise NotImplementedError
    def query_messages(self, user, stale=False):
        raise NotImplementedError
    def pop_messages(self, user, stale=False):
        raise NotImplementedError
    def add_message(self, user, message):
        raise NotImplementedError
    def query_delivery(self, msgid):
        raise NotImplementedError
    def add_delivery(self, msg, msgid, timestamp):
        raise NotImplementedError
    def get_mail_info(self, user):
        raise NotImplementedError
    def update_mail_info(self, user, address, throttle):
        raise NotImplementedError
    def update_mail_throttle(self, user, throttle):
        raise NotImplementedError
    def init_setting(self, key, value):
        raise NotImplementedError
    def get_setting(self, key):
        raise NotImplementedError
    def set_setting(self, key, value):
        raise NotImplementedError
    def gc(self):
        raise NotImplementedError

class NotificationDistributorMemory(NotificationDistributor):
    def __init__(self):
        self.aliases = {}
        self.revaliases = {}
        self.seen = {}
        self.messages = {}
        self.deliveries = {}
        self.groups = {}
        self.revgroups = {}
        self.groupdescs = {}
        self.mailinfo = {}
        self.settings = {}
        self.lock = threading.RLock()

    def __enter__(self):
        self.lock.__enter__()
    def __exit__(self, t, v, tb):
        self.lock.__exit__(t, v, tb)

    def query_user(self, name):
        ret = self.normalize_user(name)
        try:
            return (self.revaliases[ret[0]], ret[1])
        except KeyError:
            return ret

    def query_aliases(self, base):
        with self.lock:
            return self.aliases.get(base, [])

    def update_aliases(self, base, names):
        with self.lock:
            # Remove backreferences.
            for n in self.aliases.pop(base, ()):
                self.revaliases.pop(n[0], None)
            # Ensure names is not empty.
            if not names: return (None, names)
            # Absorb other aliases.
            nn = OrderedSet.firstel(names)
            seen = set()
            for n in [x[0] for x in nn]: # Avoid concurrent modification.
                k = self.revaliases.get(n, n)
                if k in seen: continue
                seen.add(k)
                nn.extend(self.aliases.pop(k, ()))
            # Choose new base if necessary.
            if (base, None) not in nn: base = names[0][0]
            # Install alias table.
            self.aliases[base] = list(nn)
            # Install backreferences.
            for n, r in nn: self.revaliases[n] = base
            # Return new values.
            return (base, self.aliases[base])

    def query_seen(self, user):
        with self.lock:
            base = self.revaliases.get(user, user)
            entry, unread = None, 0
            for k, n in self.aliases.get(base, ((user, None),)):
                e = self.seen.get(k)
                if not e: continue
                if entry is None or e[1] is not None and e[1] > entry[1]:
                    entry = e
                unread += e[2]
            if not entry: return None
            return (entry[0], entry[1], unread, entry[3])

    def update_seen(self, user, name, time, unread, room):
        with self.lock:
            oldent = self.seen.get(user, (None, None, 0, None))
            self.seen[user] = [name, time,
                oldent[2] if unread is None else unread, room]
            return (unread != oldent[2])

    def list_groups(self):
        with self.lock:
            return list(self.groups)

    def query_groups_of(self, user):
        with self.lock:
            ret = set()
            base = self.revaliases.get(user, user)
            for a in self.aliases.get(base, ((user, None),)):
                ret.update(self.revgroups[a[0]])
            return sorted(ret)

    def query_group(self, name, raw=False):
        with self.lock:
            if raw: return self.groups.get(name, [])
            return list(OrderedSet.deduplicate(self.groups.get(name, []),
                key=lambda x: self.revaliases.get(x[0], x[0])))

    def update_group(self, name, members):
        with self.lock:
            for e in self.groups.get(name, ()):
                g = self.revgroups[e[0]]
                g.discard(name)
            self.groups[name] = members
            for e in members:
                try:
                    g = self.revgroups[e[0]]
                except KeyError:
                    g = set()
                    self.revgroups[e[0]] = g
                g.add(name)
            return self.query_group(name)

    def query_groupdesc(self, name):
        with self.lock:
            return self.groupdescs.get(name)

    def update_groupdesc(self, name, description):
        with self.lock:
            self.groupdescs[name] = description

    def message_bounds(self, user):
        with self.lock:
            msgs = self.query_messages(user)
            if not msgs: return (0, None, None)
            return (len(msgs), min(m['timestamp'] for m in msgs),
                    max(m['timestamp'] for m in msgs))

    def query_messages(self, user, stale=False):
        with self.lock:
            base = self.revaliases.get(user, user)
            names = self.aliases.get(base, ((user, None),))
            msgs = []
            for n in names: msgs.extend(self.messages.get(n[0], ()))
            msgs.sort(key=operator.itemgetter('timestamp'))
            return msgs

    def pop_messages(self, user, stale=False):
        with self.lock:
            base = self.revaliases.get(user, user)
            names = self.aliases.get(base, ((user, None),))
            msgs = []
            for n in names: msgs.extend(self.messages.pop(n[0], ()))
            msgs.sort(key=operator.itemgetter('timestamp'))
            return list(msgs)

    def add_message(self, user, message):
        message['id'] = id(message)
        message['to'] = user
        with self.lock:
            self.messages.setdefault(user, []).append(message)

    def query_delivery(self, msgid):
        with self.lock:
            return self.deliveries.get(msgid)

    def add_delivery(self, msg, msgid, timestamp):
        with self.lock:
            msg['delivered_to'] = msgid
            msg['delivered'] = timestamp
            self.deliveries[msgid] = msg

    def get_mail_info(self, user):
        with self.lock:
            return self.mailinfo.get(user)

    def update_mail_info(self, user, address, throttle):
        with self.lock:
            self.mailinfo[user] = [address, throttle]

    def update_mail_throttle(self, user, throttle):
        with self.lock:
            entry = self.mailinfo.get(user)
            if not entry or (entry[1] is not None and entry[1] >= throttle):
                return
            entry[1] = throttle

    def init_setting(self, key, value):
        with self.lock:
            self.settings.setdefault(key, value)

    def get_setting(self, key):
        with self.lock:
            return self.settings.get(key)

    def set_setting(self, key, value):
        with self.lock:
            self.settings[key] = value

    def gc(self):
        deadline = time.time() - REPLY_TIMEOUT
        with self.lock:
            for k, v in tuple(self.deliveries.items()):
                if v['delivered'] < deadline:
                    del self.deliveries[k]

class NotificationDistributorSQLite(NotificationDistributor):
    def __init__(self, filename):
        self.filename = filename
        self.lock = DBLock(None)
        self.conn = None
        self.curs = None
        self.init()

    def __enter__(self):
        self.lock.__enter__()
    def __exit__(self, t, v, tb):
        self.lock.__exit__(t, v, tb)

    def init(self):
        with self.lock.committing:
            self.conn = sqlite3.connect(self.filename, isolation_level='',
                                        check_same_thread=False)
            self.curs = self.conn.cursor()
            self.lock.conn = self.conn
            # Message table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS messages ('
                                  'sender TEXT, '
                                  'recipient TEXT, '
                                  'reason TEXT, '
                                  'text TEXT, '
                                  'timestamp REAL, '
                                  'delivered_to TEXT UNIQUE, '
                                  'delivered REAL, '
                                  'priority TEXT, '
                                  'room TEXT'
                              ')')
            # Group table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS groups ('
                                  'groupname TEXT, '
                                  'member TEXT, '
                                  'name TEXT, '
                                  'PRIMARY KEY (groupname, member)'
                              ')')
            # Group description table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS groupdescs ('
                                  'groupname TEXT PRIMARY KEY, '
                                  'description TEXT'
                              ')')
            # Seen table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS seen ('
                                  'user TEXT PRIMARY KEY, '
                                  'name TEXT, '
                                  'timestamp REAL, '
                                  'unread INTEGER, '
                                  'room TEXT'
                              ')')
            # Alias table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS aliases ('
                                  'base TEXT, '
                                  'user TEXT PRIMARY KEY, '
                                  'name TEXT'
                              ')')
            # Mail table.
            # user     is the alias base of the user,
            # address  is the full email address (@-mentions get mis-parsed),
            # throttle is the time when one may send again (or NULL).
            # Inform users that changing their primary alias may prevent them
            # from getting mail.
            self.curs.execute('CREATE TABLE IF NOT EXISTS mailinfo ('
                                  'user TEXT PRIMARY KEY, '
                                  'address TEXT, '
                                  'throttle REAL'
                              ')')
            # Configuration table.
            self.curs.execute('CREATE TABLE IF NOT EXISTS settings ('
                                  'name TEXT PRIMARY KEY, '
                                  'value TEXT'
                              ')')
            # Schema upgrades.
            self.curs.execute('PRAGMA table_info(seen);')
            seencols = set(i[1] for i in self.curs.fetchall())
            for coldesc in ('unread INTEGER', 'room TEXT'):
                if coldesc.partition(' ')[0] not in seencols:
                    self.curs.execute('ALTER TABLE seen '
                        'ADD COLUMN ' + coldesc)
            self.curs.execute('PRAGMA table_info(messages);')
            msgcols = set(i[1] for i in self.curs.fetchall())
            for coldesc in ('priority TEXT', 'room TEXT'):
                if coldesc.partition(' ')[0] not in msgcols:
                    self.curs.execute('ALTER TABLE messages '
                        'ADD COLUMN ' + coldesc)

    def _unwrap_message(self, item):
        return {'id': item[0], 'from': item[1], 'to': item[2],
                'reason': item[3], 'text': item[4], 'timestamp': item[5],
                'delivered_to': item[6], 'delivered': item[7],
                'priority': item[8], 'room': item[9]}
    def _unwrap_messages(self, it):
        return list(map(self._unwrap_message, it))
    def _wrap_message(self, message):
        return (message.get('id'), message['from'], message['to'],
                message['reason'], message['text'], message['timestamp'],
                message.get('delivered_to'), message.get('delivered'),
                message.get('priority'), message.get('room'))

    def query_user(self, name):
        ret = self.normalize_user(name)
        with self.lock:
            self.curs.execute('SELECT base FROM aliases WHERE user = ?',
                              (ret[0],))
            res = self.curs.fetchone()
            if res: return (res[0], ret[1])
        return ret

    def query_aliases(self, base):
        with self.lock:
            self.curs.execute('SELECT user, name FROM aliases '
                'WHERE base = ? ORDER BY _rowid_', (base,))
            return self.curs.fetchall()

    def update_aliases(self, base, names):
        with self.lock.committing:
            # Discard old aliases.
            self.curs.execute('DELETE FROM aliases WHERE base = ?', (base,))
            # Shortcut if there are no aliases to be added.
            if not names: return (None, names)
            # Merge in other aliases if desired.
            nn = OrderedSet.firstel(names)
            for n in [x[0] for x in nn]: # Concurrent modification.
                self.curs.execute('SELECT user, name FROM aliases '
                    'WHERE base = (SELECT base FROM aliases WHERE user = ?) '
                    'ORDER BY _rowid_', (n,))
                nn.extend(self.curs.fetchall())
            # Check if we need a new base.
            if (base, None) not in nn: base = names[0][0]
            # Poke all that back into the DB.
            self.curs.executemany('INSERT OR REPLACE INTO aliases '
                'VALUES (?, ?, ?)', ((base, n, m) for n, m in nn))
            # Return new values.
            return (base, list(nn))

    def query_seen(self, user):
        with self.lock:
            self.curs.execute('SELECT name, timestamp, unread, room '
                'FROM seen WHERE user IN (SELECT user FROM aliases '
                    'WHERE base = (SELECT base FROM aliases WHERE user = ?) '
                'UNION SELECT ?)', (user, user))
            entry, unread = None, 0
            for e in self.curs.fetchall():
                if entry is None or e[1] is not None and e[1] > entry[1]:
                    entry = e
                unread += e[2]
            if not entry: return None
            return (entry[0], entry[1], unread, entry[3])

    def update_seen(self, user, name, timestamp, unread, room):
        with self.lock.committing:
            self.curs.execute('SELECT unread FROM seen WHERE user = ?',
                              (user,))
            old_unread = self.curs.fetchone()
            if old_unread is None or old_unread[0] is None: old_unread = (0,)
            if unread is None: unread = old_unread[0]
            self.curs.execute('INSERT OR REPLACE INTO seen '
                'VALUES (?, ?, ?, ?, ?)',
                (user, name, timestamp, unread, room))
            return (old_unread[0] != unread)

    def list_groups(self):
        with self.lock:
            self.curs.execute('SELECT DISTINCT groupname FROM groups')
            return [i[0] for i in self.curs.fetchall()]

    def query_groups_of(self, user):
        with self.lock:
            self.curs.execute('SELECT DISTINCT groupname FROM groups '
                'WHERE member IN (SELECT user FROM aliases '
                    'WHERE base = (SELECT base FROM aliases WHERE user = ?) '
                'UNION SELECT ?)', (user, user))
            return sorted(x[0] for x in self.curs.fetchall())

    def query_group(self, name, raw=False):
        with self.lock:
            # base is redacted out by the following code
            self.curs.execute('SELECT base, member, groups.name FROM groups '
                'LEFT JOIN aliases ON member = user WHERE groupname = ? '
                'ORDER BY groups._rowid_', (name,))
            if raw: return [x[1:] for x in self.curs.fetchall()]
            return list(OrderedSet.deduplicate(self.curs.fetchall(),
                key=lambda x: x[0] or x[1], map=lambda x: x[1:]))

    def update_group(self, name, members):
        with self.lock.committing:
            self.curs.execute('DELETE FROM groups WHERE groupname = ?',
                              (name,))
            self.curs.executemany('INSERT INTO groups VALUES (?, ?, ?)',
                                  ((name, m, n) for m, n in members))
            return self.query_group(name)

    def query_groupdesc(self, name):
        with self.lock:
            self.curs.execute('SELECT description FROM groupdescs '
                'WHERE groupname = ?', (name,))
            res = self.curs.fetchone()
            return res[0] if res else None

    def update_groupdesc(self, name, description):
        with self.lock.committing:
            self.curs.execute('INSERT OR REPLACE INTO groupdescs '
                'VALUES (?, ?)', (name, description))

    def message_bounds(self, user):
        with self.lock:
            self.curs.execute('SELECT COUNT(*), MIN(timestamp), '
                'MAX(timestamp) FROM messages '
                'WHERE recipient IN (SELECT user FROM aliases '
                    'WHERE base = (SELECT base FROM aliases WHERE user = ?) '
                'UNION SELECT ?) AND delivered IS NULL', (user, user))
            return self.curs.fetchone()

    def query_messages(self, user, stale=False):
        with self.lock:
            query = ('SELECT _rowid_, * FROM messages '
                'WHERE recipient IN (SELECT user FROM aliases '
                    'WHERE base = (SELECT base FROM aliases WHERE user = ?) '
                'UNION SELECT ?) %s ORDER BY timestamp') % (
                '' if stale else 'AND delivered IS NULL')
            self.curs.execute(query, (user, user))
            return self._unwrap_messages(self.curs.fetchall())

    def pop_messages(self, user, stale=False):
        with self.lock.committing:
            query = ('SELECT _rowid_, * FROM messages '
                'WHERE recipient IN (SELECT user FROM aliases WHERE base = '
                    '(SELECT base FROM aliases WHERE user = ?) '
                    'UNION SELECT ?) %s ORDER BY timestamp') % (
                '' if stale else 'AND delivered IS NULL')
            self.curs.execute(query, (user, user))
            msgs = self.curs.fetchall()
            now = time.time()
            self.curs.executemany('UPDATE messages SET delivered = ? '
                'WHERE _rowid_ = ? AND delivered IS NULL',
                ((now, i[0]) for i in msgs))
            return self._unwrap_messages(msgs)

    def add_message(self, user, message):
        message['to'] = user
        with self.lock.committing:
            self.curs.execute('INSERT INTO messages '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                self._wrap_message(message)[1:])

    def query_delivery(self, msgid):
        with self.lock:
            self.curs.execute('SELECT _rowid_, * FROM messages '
                'WHERE delivered_to = ?', (msgid,))
            res = self.curs.fetchone()
            if res is None: return None
            return self._unwrap_message(res)

    def add_delivery(self, msg, msgid, timestamp):
        with self.lock.committing:
            self.curs.execute('UPDATE messages SET delivered_to = ?, '
                'delivered = ? WHERE _rowid_ = ?', (msgid, timestamp,
                                                    msg['id']))

    def get_mail_info(self, user):
        with self.lock:
            self.curs.execute('SELECT address, throttle FROM mailinfo '
                'WHERE user = ?', (user,))
            return self.curs.fetchone()

    def update_mail_info(self, user, address, throttle):
        with self.lock:
            self.curs.execute('INSERT OR REPLACE INTO mailinfo '
                'VALUES (?, ?, ?)', (user, address, throttle))

    def update_mail_throttle(self, user, throttle):
        with self.lock.committing:
            self.curs.execute('UPDATE mailinfo SET throttle = ? '
                'WHERE user = ? AND (throttle IS NULL OR throttle < ?)',
                (throttle, user, throttle))

    def init_setting(self, key, value):
        with self.lock.committing:
            self.curs.execute('INSERT OR IGNORE INTO settings VALUES '
                '(?, ?)', (key, value))

    def get_setting(self, key):
        with self.lock:
            self.curs.execute('SELECT value FROM settings WHERE name = ?',
                              (key,))
            res = self.curs.fetchone()
            return None if res is None else res[0]

    def set_setting(self, key, value):
        with self.lock.committing:
            self.curs.execute('INSERT OR REPLACE INTO settings VALUES '
                '(?, ?)', (key, value))

    def gc(self):
        deadline = time.time() - REPLY_TIMEOUT
        with self.lock.committing:
            self.curs.execute('DELETE FROM messages WHERE delivered < ?',
                              (deadline,))

class Mailer:
    @classmethod
    def extract_addrspec(cls, address):
        m = re.match('^[^<]+ <([^>]+)>$', address)
        return m.group(1) if m else None

    @classmethod
    def init_settings(cls, distr):
        # Send mail?
        distr.init_setting('mail', 'no')
        # What to send mail with (currently only "sendmail")
        distr.init_setting('mail.backend', 'sendmail')
        # Sender address ("TellBot <tellbot@example.com>")
        distr.init_setting('mail.from', None)
        # Envelope sender address (derived from mail.from as default; no
        # angled brackets)
        distr.init_setting('mail.realfrom', None)
        # Tag to prepend to the auto-generated subject in square brackets
        distr.init_setting('mail.subjtag', None)
        # Which command to use as sendmail
        distr.init_setting('mail.sendmail.command', 'sendmail')

    def __init__(self, distr):
        self.distr = distr

    def allow_send(self, message):
        info = self.distr.get_mail_info(message['to'])
        if info is None or message.get('priority') == 'LOW':
            return False
        elif (message.get('priority') != 'URGENT' and info[1] is not None and
              info[1] > time.time()):
            return False
        else:
            return True

    def format_send(self, message):
        def asciienc(s):
            return s.encode('ascii').decode('ascii')
        def utfenc(s):
            return s
        def htmlenc(s):
            return escape(s).encode('ascii',
                errors='xmlcharrefreplace').decode('ascii')
        minfo = self.distr.get_mail_info(message['to'])
        binfo = self.distr.message_bounds(message['to'])
        full_from = self.distr.get_setting('mail.from')
        if full_from is None:
            raise RuntimeError('mail.from not configured')
        real_from = self.distr.get_setting('mail.realfrom')
        if real_from is None:
            real_from = self.extract_addrspec(full_from)
            if real_from is None:
                raise RuntimeError('Ill-formatted mail.from')
        real_to = self.extract_addrspec(minfo[0])
        if real_to is None:
            raise ValueError('Ill-formatted recipient address')
        msg_priority = message['priority']
        subject = 'New%s TellBot message (%s unread)' % (
            (' urgent' if msg_priority == 'URGENT' else ''), binfo[0])
        subjtag = self.distr.get_setting('mail.subjtag')
        if subjtag is not None: subject = '[%s] %s' % (subjtag, subject)
        msg_from = make_mention(message['from'])
        msg_to = message['reason']
        return (real_from, real_to, (EMAIL_NOTIFICATION_TEMPLATE % {
            'from': asciienc(full_from),
            'to': asciienc(minfo[0]),
            'subject': asciienc(subject),
            'boundary': base64.b64encode(os.urandom(16)).decode('ascii'),
            'unread_total': binfo[0],
            'plain_from': utfenc(msg_from),
            'plain_to': utfenc(msg_to),
            'plain_prio': utfenc(msg_priority),
            'plain_text': utfenc(message['text']),
            'html_from': htmlenc(msg_from),
            'html_to': htmlenc(msg_to),
            'html_prio': htmlenc(msg_priority),
            'html_text': htmlenc(message['text']).replace('\n', '<br/>')
        }).encode('utf-8'))

    def send(self, message):
        raise NotImplementedError

class MailerNull(Mailer):
    def allow_send(self, message):
        return False

    def send(self, message):
        return None

class MailerSendmail(Mailer):
    def send(self, message):
        sender, recipient, data = self.format_send(message)
        cmd = self.distr.get_setting('mail.sendmail.command')
        proc = subprocess.Popen([cmd, '-f', sender, recipient],
                                stdin=subprocess.PIPE)
        proc.stdin.write(re.sub(b'(?m)^\.', b'..', data) + b'\n.\n')
        proc.stdin.close()
        if proc.wait() == 0:
            return (sender, recipient, data)
        else:
            return None

class TellBot(basebot.Bot):
    BOTNAME = 'TellBot'
    NICKNAME = 'TellBot'
    SHORT_HELP = 'I can schedule messages to be delivered to other users.'
    LONG_HELP = HELP_TEXT

    @classmethod
    def init_settings(cls, distr):
        # NotBot fallback mode
        distr.init_setting('nbfallback', 'no')

    def __init__(self, *args, **kwds):
        basebot.Bot.__init__(self, *args, **kwds)
        self._tasklock = threading.RLock()
        self._runner = None
        self._task_queue = None
        self._pending = {}

    def _format_nick(self, nick, ping=True, subject=None, title=False):
        nnick = basebot.normalize_nick(nick)
        ttr = (titlefirst if title else lambda x: x)
        if nnick == basebot.normalize_nick(self.nickname):
            return ttr('me')
        elif subject and nnick == basebot.normalize_nick(subject):
            return ttr('you')
        else:
            return (make_mention if ping else seminormalize_nick)(nick)

    def _format_users(self, users, groups, subject, prevent_self=False,
                      ping=True):
        if not users: return ('no-one', {})
        tr = lambda x: self._format_nick(x[1], ping, subject[1])
        seen, segnames, segments, reasons = set(), [], {}, {}
        for n, c in groups.items():
            if n.startswith('@'):
                el = c[0]
                if reasons.get(el[0], '').startswith('*'):
                    del segments[reasons[el[0]]][el[0]]
                reasons[el[0]] = n
                segnames.append(tr(el))
                seen.add(n)
            else:
                nc = [i for i in c if i[0] not in seen]
                if subject in nc and prevent_self:
                    nc.remove(subject)
                    reasons.pop(subject[0], None)
                    users.discard(subject)
                for normnick, nick in nc:
                    reasons[normnick] = n
                segnames.append(n)
                segments[n] = collections.OrderedDict(
                    (i[0], tr(i)) for i in nc)
                seen.update(i[0] for i in nc)
        parts = []
        for n in segnames:
            if n not in segments:
                parts.append(n)
                continue
            names = list(segments[n].values())
            if not groups[n]:
                names.append('-empty-')
            elif len(names) != len(groups[n]):
                names.append('...')
            parts.append('%s (%s)' % (n, format_list(names)))
        return (format_list(parts, 'no-one'), reasons)

    def _spawn_task_runner(self):
        self._task_queue = Queue()
        self._runner = basebot.spawn_thread(self._task_runner)

    def _task_runner(self):
        queue = self._task_queue
        while 1:
            task = queue.get()
            try:
                time.sleep(task.time - time.time())
            except ValueError:
                pass
            try:
                with self._tasklock:
                    self._pending.pop(task.id, None)
                if not task.canceled: task()
            finally:
                queue.task_done()

    def _schedule_task(self, delay, func, *args, **kwds):
        tid = kwds.pop('_id')
        t = lambda: func(*args, **kwds)
        t.time = time.time() + delay
        t.canceled = False
        t.id = tid
        with self._tasklock:
            if tid: self._pending[tid] = t
            self._task_queue.put(t)

    def _cancel_task(self, tid):
        with self._tasklock:
            try:
                self._pending[tid].canceled = True
            except KeyError:
                pass

    def handle_chat_ex(self, msg, meta):
        basebot.Bot.handle_chat_ex(self, msg, meta)
        distr, reply = self.manager.distributor, meta['reply']
        user, now = distr.normalize_user(msg['sender']['name']), time.time()

        # Update online time database.
        if meta['edit'] or meta['long']: return
        unread, oldest, newest = distr.message_bounds(user[0])
        update = distr.update_seen(user[0], user[1], now, unread,
                                   self.roomname)
        distr.update_mail_throttle(user[0], time.time() + MAIL_SEEN_COOLOFF)

        # Prevent NotBot fallback from firing.
        if msg['sender']['session_id'] != self.session_id:
            self._cancel_task(msg['parent'])

        # Deliver messages to myself.
        if msg['sender']['session_id'] == self.session_id:
            messages = distr.pop_messages(user[0])
            if len(messages) == 1:
                reply('/me read 1 message.')
            elif messages:
                reply('/me read %s messages.' % len(messages))
        elif update:
            if re.match(r'!(inbox|boop)\b', msg['content']):
                pass
            elif oldest is not None and oldest >= now - INBOX_CUTOFF:
                self.deliver_notifies(distr, user, reply, False)
            elif unread == 1:
                reply('You have 1 unread message; use !inbox to read it. ' +
                      REPLY_HELP)
            elif unread > 1:
                reply(('You have %s unread messages; use !inbox to read '
                       'them. ' % unread) + REPLY_HELP)

    def send_notify(self, sender, recipients, groups, text, reply,
                    reason=None, priority='normal', ping=False):
        distr, mailer = self.manager.distributor, self.manager.mailer

        # Prevent messages to oneself unless explicit.
        reclist, reasons = self._format_users(recipients, groups, sender,
                                              True, ping)

        # Format fancy recipient list.
        text = (text or '').strip()
        if not text:
            reply('Nothing to tell %s (did you specify a message?).' %
                  reclist)
            return

        # Schedule messages.
        base = {'text': text, 'from': sender[1], 'timestamp': time.time(),
                'priority': priority, 'room': self.roomname}
        for user, nick in recipients:
            cur_reason = reason or reasons[user]
            message = dict(base, to=user, tonick=nick, reason=cur_reason)
            distr.add_message(user, message)
            try:
                if mailer.allow_send(message):
                    res = mailer.send(message)
                    distr.update_mail_throttle(user, base['timestamp'] +
                                               MAIL_SEND_COOLOFF)
                    if res is None:
                        self.logger.info('Sending mail to @%s failed.' %
                                         nick)
                    else:
                        self.logger.info('Sent mail to @%s <%s>.' %
                                         (nick, res[1]))
            except Exception as e:
                self.logger.error('Error while sending mail', exc_info=True)

        # Reply.
        reply('Will tell %s.' % reclist)

    def deliver_notifies(self, distr, sender, reply, stale=False):
        # Format a delivery reason.
        def format_reason(src):
            if src.startswith('<re> '):
                return 'replying ' + format_reason(src[5:])
            elif src.startswith('@'):
                return 'to ' + self._format_nick(src[1:], False, sender[1])
            else:
                return 'to ' + src

        # Actually deliver a message.
        def deliver_message():
            # Add a delivery notice.
            def handle_delivery(reply):
                distr.add_delivery(m, reply.data.id, reply.data.time)
                # HACK: Wait a bit to avoid being kicked for spamming.
                if len(messages) > 10: time.sleep(1)
                deliver_message()
            try:
                m = next(msgitr)
            except StopIteration:
                return
            if m['reason'] == make_mention(sender[1]):
                reason = ''
            else:
                reason = ' ' + format_reason(m['reason'])
            roomname = m.get('room')
            if roomname is None or roomname == self.roomname:
                room = ''
            else:
                room = ' from &%s' % roomname
            reply('[%s%s%s, %s ago] %s' % (
                self._format_nick(m['from'], False, sender[1], True),
                reason, room,
                basebot.format_delta(time.time() - m['timestamp'], False),
                m['text']), handle_delivery)

        # Deliver messages.
        now = time.time()
        messages = distr.pop_messages(sender[0], stale)
        msgitr = iter(messages)
        deliver_message()
        distr.update_seen(sender[0], sender[1], now, 0,
                          self.roomname)

        # ...Or none.
        if not messages:
            reply('No mail.')

    def handle_command(self, cmdline, meta):
        basebot.Bot.handle_command(self, cmdline, meta)
        self.process_command(cmdline, meta)

    def process_command(self, cmdline, meta):
        # Common part of the argument parsers.
        def parse_userlist(base, groups, it, userpol='normal',
                           grouppol='normal'):
            def check_policy(t, x):
                if t == 'user':
                    policy, othpolicy, ot = userpol, grouppol, 'group'
                else:
                    policy, othpolicy, ot = grouppol, userpol, 'user'
                if policy == 'none':
                    reply('Please do not specify ' + t + 's.')
                    return Ellipsis, count
                elif policy == 'get':
                    if x:
                        reply('Please specify a ' + t + ' first.')
                        return Ellipsis, count
                    return arg, count
                elif othpolicy == 'get':
                    reply('Please specify a ' + ot + ' first.')
                    return Ellipsis, count
            count = 0
            for arg in it:
                if arg.startswith('@'): # Add user.
                    r = check_policy('user', False)
                    if r: return r
                    u = distr.normalize_user(arg[1:])
                    base.append(u)
                    groups[arg] = [u]
                    count += 1
                elif arg.startswith('*'): # Add group.
                    r = check_policy('group', False)
                    if r: return r
                    g = distr.query_group(arg[1:])
                    base.extend(g)
                    groups[arg] = g
                    count += 1
                elif arg.startswith('+@'): # Add user (long form).
                    r = check_policy('user', True)
                    if r: return r
                    u = distr.normalize_user(arg[2:])
                    base.append(u)
                    groups[arg[1:]] = [u]
                    count += 1
                elif arg.startswith('+*'): # Add group (long form).
                    r = check_policy('group', True)
                    if r: return r
                    g = distr.query_group(arg[2:])
                    base.extend(g)
                    groups[arg[1:]] = g
                    count += 1
                elif arg.startswith('-@'): # Discard user.
                    r = check_policy('user', True)
                    if r: return r
                    base.discard(distr.normalize_user(arg[2:]))
                    count += 1
                elif arg.startswith('-*'): # Discard group.
                    r = check_policy('group', True)
                    if r: return r
                    base.discard_all(distr.query_group(arg[2:]))
                    count += 1
                elif arg.startswith('--'): # Option.
                    return arg, count
                elif arg.startswith('-'): # Avoid confusion with above.
                    reply('Please precede options with double dashes, or '
                          'delimit the beginning of your message with a '
                          'double dash (--).')
                    return Ellipsis, count
                else: # Start of normal arguments.
                    return arg, count
            return None, count

        # Nickname formatting for output.
        def format_nick(item, ping, title=False):
            return self._format_nick(item[1], ping, sender[1], title)

        # Reply with the users from a given list.
        def display_group(groupname, members, ping, comment):
            head = 'Members%s%s%s: ' % ((' ' if comment else ''), comment,
                (' (%s)' % len(members) if members else ''))
            tr = lambda x: format_nick(x, ping)
            lst = format_list(map(tr, members), '-none-')
            reply(head + lst)

        # Reply with the users from a given list.
        def display_aliases(base, names, ping, comment):
            altbases = [x for x in names if x[0] == base[0]]
            if altbases:
                bname = ' of @' + altbases[0][1]
            elif base[1]:
                bname = ' of @' + base[1]
                if not names: names = [base]
            else:
                bname = ''
            head = 'Aliases%s%s%s%s: ' % (bname, (' ' if comment else ''),
                comment, (' (%s)' % len(names) if names else ''))
            tr = lambda x: format_nick(x, ping)
            lst = format_list(map(tr, names), '-none-')
            reply(head + lst)

        # Accumulate a reply.
        def reply(msg):
            replybuf.append(msg)
        # Drain all replies.
        def flush(msg=None):
            if msg is not None:
                replybuf.append(msg)
            if replybuf:
                meta['reply']('\n'.join(replybuf))
                replybuf[:] = []

        distr = self.manager.distributor
        sender = distr.normalize_user(meta['sender'])
        replybuf = []

        # Ensure replies are delivered.
        try:

            # Lock database.
            distr.__enter__()

            # Send a message.
            if cmdline[0] in ('!tell', '!tnotify'):
                self._log_command(cmdline)
                # Parse arguments.
                recipients = OrderedSet.firstel()
                groups = collections.OrderedDict()
                text, priority, ping = None, 'normal', False
                it = iter(cmdline[1:])
                while 1:
                    arg, count = parse_userlist(recipients, groups, it)
                    if arg is None:
                        break
                    elif arg is Ellipsis:
                        return
                    elif arg == '--':
                        try:
                            text = meta['line'][next(it).offset:]
                        except StopIteration:
                            pass
                        break
                    elif arg == '--ping':
                        ping = True
                    elif arg.startswith('--priority'):
                        if len(arg) <= 10:
                            try:
                                priority = next(it)
                                continue
                            except StopIteration:
                                reply('Missing message priority.')
                                return
                        elif arg[10] != '=':
                            reply('Unknown option %s.' % arg)
                            return
                        elif len(arg) <= 11:
                            reply('Missing message priority.')
                            return
                        priority = arg[11:]
                    elif arg.startswith('--'):
                        reply('Unknown option %s.' % arg)
                        return
                    else:
                        text = meta['line'][arg.offset:]
                        break

                priority = priority.upper()
                if priority not in ('LOW', 'NORMAL', 'URGENT'):
                    reply('Unknown priority %s.' % priority)
                    return
                elif (priority == 'URGENT' and
                        not meta['msg'].sender.is_manager and
                        not meta['msg'].sender.is_staff):
                    reply('Only room hosts may send urgent messages.')
                    return

                # Actual hauling outlined into own function.
                self.send_notify(sender, recipients, groups, text, reply,
                                 priority=priority, ping=ping)

            # @NotBot compatibility.
            elif cmdline[0] == '!notify':
                # HACK: Monkey-patching shorter command into command line.
                nbfallback = distr.get_setting('nbfallback')
                if nbfallback == 'yes':
                    self.process_command(['!tell'] + cmdline[1:], meta)
                elif nbfallback == 'wait':
                    self._schedule_task(NOTBOT_DELAY, self.process_command,
                        ['!tell'] + cmdline[1:], meta, _id=meta['msgid'])
                elif nbfallback.isdigit():
                    self._schedule_task(int(nbfallback, 10),
                        self.process_command, ['!tell'] + cmdline[1:], meta,
                        _id=meta['msgid'])

            # Reply to a freshly delivered message.
            elif cmdline[0] == '!reply':
                self._log_command(cmdline)
                # Determine recipient.
                if meta['msg']['parent'] is None:
                    reply('Nothing to reply to.')
                    return
                cause = distr.query_delivery(meta['msg']['parent'])
                if cause is None:
                    reply('Message not recognized.')
                    return
                recipient = distr.normalize_user(cause['from'])

                # Send message.
                self.send_notify(
                    sender,
                    OrderedSet.firstel((recipient,)),
                    {'@' + recipient[0]: [recipient]},
                    meta['line'][cmdline[1].offset:],
                    reply,
                    reason='<re> ' + make_mention(recipient[1]))

            # Reply to a group.
            elif cmdline[0] == '!reply-all':
                self._log_command(cmdline)
                # Determine recipient.
                if meta['msg']['parent'] is None:
                    reply('Nothing to reply to.')
                    return
                cause = distr.query_delivery(meta['msg']['parent'])
                if cause is None:
                    reply('Message not recognized.')
                    return
                reason = cause['reason']
                if reason.startswith('<re> '): reason = reason[5:]

                # Determine group members.
                if reason.startswith('@'):
                    groups = {reason: [distr.normalize_user(reason[1:])]}
                else:
                    groups = {reason: distr.query_group(reason[1:])}
                recipients = OrderedSet.firstel(groups[reason])

                # Send message.
                self.send_notify(sender, recipients, groups,
                    meta['line'][cmdline[1].offset:], reply,
                    reason='<re> ' + reason)

            # Enumerate available groups.
            elif cmdline[0] == '!tlistgroups':
                self._log_command(cmdline)
                # Parse arguments.
                if len(cmdline) == 1:
                    filt = lambda x: True
                    filt_all = True
                elif len(cmdline) == 2:
                    regex = re.compile(fnmatch.translate(cmdline[1]), re.I)
                    filt = regex.match
                    filt_all = False
                else:
                    reply('Please specify a matching pattern or nothing.')
                    return

                # Obtain list.
                names = ['*' + i for i in distr.list_groups() if filt(i)]
                names.sort(key=lambda x: x.lower())

                if not names:
                    reply('No groups.' if filt_all else
                          'No groups matching pattern.')
                    return

                # Group by first character.
                groups = []
                for n in names:
                    if not groups:
                        groups.append([n])
                    elif n[:2].lower() != groups[-1][-1][:2].lower():
                        groups.append([n])
                    else:
                        groups[-1].append(n)

                # Output.
                reply('\n'.join(map(', '.join, groups)))

            # List the groups a user is a member of.
            elif cmdline[0] == '!tgroupsof':
                self._log_command(cmdline)
                # Parse arguments.
                users, ping = OrderedSet.firstel(), False
                it = iter(cmdline[1:])
                while 1:
                    arg, cnt = parse_userlist(users, {}, it)
                    if arg is None:
                        break
                    elif arg is Ellipsis:
                        return
                    elif arg == '--ping':
                        ping = True
                    elif arg.startswith('--'):
                        reply('Unknown option %s.' % arg)
                        return

                # Handle empty list.
                if not users:
                    reply('No-one to look for.')
                    return

                # Actually output into.
                for user, nick in users:
                    groups = sorted(distr.query_groups_of(user))
                    count = ' (%s)' % len(groups) if groups else ''
                    reply('Groups of %s%s: %s' % (format_nick((user, nick),
                        ping), count, format_list(['*' + i for i in groups],
                        '-none-')))

            # Update or list a group.
            elif cmdline[0] in ('!tgroup', '!tungroup', '!tgrouplist'):
                self._log_command(cmdline)
                # Parse arguments.
                groupname, members, groups, ping = None, None, None, False
                newdesc, it, count = None, iter(cmdline[1:]), 0
                while 1:
                    arg, cnt = parse_userlist(members, groups, it,
                        grouppol=('get' if groupname is None else 'normal'))
                    count += cnt
                    if arg is None:
                        break
                    elif arg is Ellipsis:
                        return
                    elif arg.startswith('*'):
                        groupname = arg[1:]
                        old_members = distr.query_group(groupname)
                        if cmdline[0] == '!tgroup':
                            members = OrderedSet.firstel(old_members)
                        else:
                            members = OrderedSet.firstel()
                        groups = {}
                    elif arg == '--':
                        try:
                            newdesc = meta['line'][next(it).offset:].strip()
                        except StopIteration:
                            newdesc = ''
                        break
                    elif arg == '--ping':
                        ping = True
                    elif arg.startswith('--'):
                        reply('Unknown option %s.' % arg)
                        return
                    else:
                        reply('Please specify only group changes or a '
                              'single group name to display members of. '
                              '(%s)' % USERSPEC_HELP)
                        return
                if groupname is None:
                    if cmdline[0] == '!tgrouplist':
                        reply('Please specify a group to show.')
                    else:
                        reply('Please specify a group to show or change.')
                    return
                elif cmdline[0] == '!tgrouplist' and (newdesc is not None or
                                                      count != 0):
                    reply('Use !tgroup to edit a group.')
                    return

                # Reply heading.
                reply('Group: *%s' % groupname)

                # Update description.
                if newdesc:
                    olddesc = distr.query_groupdesc(groupname)
                    if olddesc:
                        reply('Old description: ' +
                              olddesc.replace('\n', '\n    '))
                    distr.update_groupdesc(groupname, newdesc)
                    reply('New description: ' +
                          newdesc.replace('\n', '\n    '))
                else:
                    desc = distr.query_groupdesc(groupname)
                    if desc:
                        reply('Description: ' +
                              desc.replace('\n', '\n    '))

                # Display old membership.
                display_group(groupname, old_members, ping,
                              ('' if count == 0 else 'before'))

                # Apply changes.
                if count != 0:
                    if cmdline[0] == '!tungroup':
                        removes = members
                        members = OrderedSet.firstel(old_members)
                        members.discard_all(removes)
                    nmembers = distr.update_group(groupname, list(members))
                    display_group(groupname, nmembers, ping, 'after')

            # Update a user's aliases.
            elif cmdline[0] in ('!alias', '!unalias'):
                self._log_command(cmdline)
                # Parse arguments.
                base, names, old_names, ping = None, None, None, False
                it, count = iter(cmdline[1:]), 0
                while 1:
                    arg, cnt = parse_userlist(names, {}, it,
                        userpol=('get' if base is None else 'normal'),
                        grouppol='none')
                    count += cnt
                    if arg is None:
                        break
                    elif arg is Ellipsis:
                        return
                    elif arg.startswith('@'):
                        base = distr.query_user(arg[1:])
                        old_names = distr.query_aliases(base[0])
                        if not old_names:
                            old_names = [distr.normalize_user(arg[1:])]
                        if cmdline[0] == '!alias':
                            names = OrderedSet.firstel(old_names)
                        else:
                            names = OrderedSet.firstel()
                    elif arg == '--ping':
                        ping = True
                    elif arg.startswith('--') and arg != '--':
                        reply('Unknown option %s.' % arg)
                        return
                    else:
                        reply('Please specify only alias changes or a '
                              'single name to display aliases of. (%s)' %
                              USERSPEC_HELP)
                        return
                if base is None:
                    if cmdline[0] == '!unalias':
                        reply('Please specify an alias to change.')
                        return
                    base = distr.query_user(sender[0])
                    old_names = distr.query_aliases(base[0])
                    if not old_names:
                        old_names = [distr.normalize_user(base[0])]
                    names = OrderedSet.firstel(old_names)
                elif cmdline[0] == '!unalias' and count == 0:
                    reply('Nothing to be done.')
                    return

                # Display old membership.
                display_aliases(base, old_names, ping,
                                ('' if count == 0 else 'before'))
                if count == 0: return

                # Apply changes.
                if cmdline[0] == '!unalias':
                    removes = names
                    names = OrderedSet.firstel(old_names)
                    names.discard_all(removes)
                nbase, nnames = distr.update_aliases(base[0], list(names))

                # Display new membership.
                if nnames:
                    display_aliases((nbase, None), nnames, ping, 'after')
                else:
                    display_aliases(base, (), ping, 'after')

            # When was a user last active?
            elif cmdline[0] == '!seen':
                self._log_command(cmdline)
                # Parse arguments.
                users, ping = OrderedSet.firstel(), False
                it = iter(cmdline[1:])
                while 1:
                    arg, cnt = parse_userlist(users, {}, it)
                    if arg is None:
                        break
                    elif arg is Ellipsis:
                        return
                    elif arg == '--ping':
                        ping = True
                    elif arg.startswith('--'):
                        reply('Please specify users or groups only.')
                        return

                # Handle empty list.
                if not users:
                    reply('No-one to check for.')
                    return

                # Output information.
                now, bnn = time.time(), basebot.normalize_nick
                for user, nick in users:
                    seen = distr.query_seen(user)
                    if seen is None: seen = (None, None, 0, None)
                    unread, oldest, newest = distr.message_bounds(user)
                    if not unread:
                        pm = ''
                    elif unread == 1:
                        pm = ' (1 pending message)'
                    else:
                        pm = ' (%s pending messages)' % unread
                    fnick = format_nick((user, nick), ping, True)
                    if seen[1] is None:
                        reply('%s not seen%s.' % (fnick, pm))
                        continue
                    if bnn(nick) != bnn(seen[0]):
                        comment = ' (as %s)' % format_nick((user, seen[0]),
                                                           ping)
                    else:
                        comment = ''
                    if seen[3] is None:
                        room = ''
                    elif seen[3] == self.roomname:
                        room = ' here'
                    else:
                        room = ' in &' + seen[3]
                    if now - seen[1] < 1:
                        delta = 'just now'
                    else:
                        delta = (basebot.format_delta(now - seen[1], False) +
                                 ' ago')
                    reply('%s%s last seen%s on %s, %s%s.' % (fnick, comment,
                        room, basebot.format_datetime(seen[1], False), delta,
                        pm))

            # Deliver pending messages.
            elif cmdline[0] in ('!inbox', '!boop'):
                self._log_command(cmdline)
                # Parse arguments
                stale = False
                for arg in cmdline[1:]:
                    if arg == '--stale':
                        stale = True
                    elif arg == '--':
                        break
                    elif arg.startswith('-'):
                        reply('Unknown option %r.' % arg)

                # Deliver messages.
                self.deliver_notifies(distr, sender, meta['reply'], stale)

        # Unlock database, deliver replies.
        finally:
            distr.__exit__(None, None, None)
            flush()

    def main(self):
        self._spawn_task_runner()
        basebot.Bot.main(self)

class GCThread(threading.Thread):
    def __init__(self, distr):
        threading.Thread.__init__(self)
        self.distr = distr
        self.exiting = False
        self.cond = threading.Condition()

    def shutdown(self):
        with self.cond:
            self.exiting = True
            self.cond.notifyAll()

    def run(self):
        cont = True
        while cont:
            self.distr.gc()
            wakeup = time.time() + GC_INTERVAL
            with self.cond:
                while not self.exiting:
                    now = time.time()
                    if now >= wakeup: break
                    self.cond.wait(wakeup - now)
                else:
                    break

class TellBotManager(basebot.BotManager):
    @classmethod
    def prepare_parser(cls, parser, config):
        basebot.BotManager.prepare_parser(parser, config)
        parser.add_argument('--db', metavar='PATH',
                            help='SQLite database file for message '
                              'persistence (default in-memory)')
        parser.add_argument('--config', action='append', dest='confopts',
                            metavar='KEY=VALUE',
                            help='A setting to apply before starting')

    @classmethod
    def interpret_args(cls, arguments, config):
        bots, config = basebot.BotManager.interpret_args(arguments, config)
        for name in ('db',):
            value = getattr(arguments, name)
            if value is not None:
                config[name] = value
        config['confopts'] = []
        for el in getattr(arguments, 'confopts') or ():
            try:
                n, v = el.split('=', 1)
            except ValueError:
                raise SystemExit('Bad configuration value: %r' % el)
            config['confopts'].append((n, v))
        return (bots, config)

    def __init__(self, **config):
        basebot.BotManager.__init__(self, **config)
        self.db = config.get('db', None)
        self.orig_conf = config.get('confopts', [])
        if self.db:
            self.distributor = NotificationDistributorSQLite(self.db)
        else:
            self.distributor = NotificationDistributorMemory()
        TellBot.init_settings(self.distributor)
        Mailer.init_settings(self.distributor)
        for n, v in self.orig_conf:
            self.distributor.set_setting(n, v)
        do_mail = self.distributor.get_setting('mail')
        mail_backend = self.distributor.get_setting('mail.backend')
        if not is_true(do_mail) or mail_backend == 'null':
            self.mailer = MailerNull(self.distributor)
        elif mail_backend == 'sendmail':
            self.mailer = MailerSendmail(self.distributor)
        else:
            raise RuntimeError('mail.backend not configured although mail '
                'is enabled')
        self.children.append(GCThread(self.distributor))

if __name__ == '__main__': basebot.run_main(TellBot, mgrcls=TellBotManager)
