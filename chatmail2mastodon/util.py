"""Utilities"""

import functools
import mimetypes
import re
import time
from argparse import Namespace
from contextlib import contextmanager
from enum import Enum
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Generator, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup
from deltachat2 import Bot, ChatType, JsonRpcError, MsgData, SpecialContactId
from html2text import html2text
from mastodon import (
    AttribAccessDict,
    Mastodon,
    MastodonNetworkError,
    MastodonServerError,
    MastodonUnauthorizedError,
)
from pydub import AudioSegment

from .orm import Account, Client, DmChat, session_scope

SPAM = [
    "/fediversechick/",
    "https://discord.gg/83CnebyzXh",
    "https://matrix.to/#/#nicoles_place:matrix.org",
]
MUTED_NOTIFICATIONS = ("reblog", "favourite", "follow")
TOOT_SEP = "\n\n―――――――――――――――\n\n"
STRFORMAT = "%Y-%m-%d %H:%M"
_scope = __name__.split(".", maxsplit=1)[0]
web = requests.Session()
web.request = functools.partial(web.request, timeout=10)  # type: ignore


class Visibility(str, Enum):
    DIRECT = "direct"  # visible only to mentioned users
    PRIVATE = "private"  # visible only to followers
    UNLISTED = "unlisted"  # public but not appear on the public timeline
    PUBLIC = "public"  # post will be public


v2emoji = {
    Visibility.DIRECT: "✉",
    Visibility.PRIVATE: "🔒",
    Visibility.UNLISTED: "🔓",
    Visibility.PUBLIC: "🌎",
}


def toots2texts(toots: Iterable) -> Generator[str, None, None]:
    for toot in toots:
        reply = toot2reply(toot)
        text = reply.text or ""
        if reply.file:
            if not text.startswith("http"):
                text = "\n" + text
            text = reply.file + "\n" + text
        sender = reply.override_sender_name or ""
        if sender:
            text = f"{sender}:\n{text}"
        if text:
            yield text


def toots2replies(bot: Bot, toots: Iterable) -> Generator:
    for toot in toots:
        reply = toot2reply(toot)
        if reply.file:
            try:
                with download_file(reply.file) as path:
                    reply.file = path
                    yield reply
                return
            except Exception as ex:
                bot.logger.exception(ex)
                text = reply.text or ""
                if not text.startswith("http"):
                    text = "\n" + text
                reply.text = reply.file + "\n" + text
                reply.file = None
        yield reply


def toot2reply(toot: AttribAccessDict) -> MsgData:
    text = ""
    reply = MsgData()
    if toot.reblog:
        reply.override_sender_name = _get_name(toot.reblog.account)
        text += f"🔁 {_get_name(toot.account)}\n\n"
        toot = toot.reblog
    else:
        reply.override_sender_name = _get_name(toot.account)

    if toot.media_attachments:
        reply.file = toot.media_attachments.pop(0).url
        text += "\n".join(media.url for media in toot.media_attachments) + "\n\n"

    soup = BeautifulSoup(toot.content, "html.parser")
    if toot.mentions:
        accts = {e.url: "@" + e.acct for e in toot.mentions}
        for anchor in soup("a", class_="u-url"):
            name = accts.get(anchor["href"], "")
            if name:
                anchor.string = name
    for linebreak in soup("br"):
        linebreak.replace_with("\n")
    for paragraph in soup("p"):
        paragraph.replace_with(paragraph.get_text() + "\n\n")
    text += soup.get_text()

    text += f"\n\n[{v2emoji[toot.visibility]} {toot.created_at.strftime(STRFORMAT)}]({toot.url})\n"
    text += f"↩️ /reply_{toot.id}\n"
    text += f"⭐ /star_{toot.id}\n"
    if toot.visibility in (Visibility.PUBLIC, Visibility.UNLISTED):
        text += f"🔁 /boost_{toot.id}\n"
    text += f"⏫ /open_{toot.id}\n"
    text += f"👤 /profile_{toot.account.id}\n"

    reply.text = text
    return reply


def notif2replies(toots: Iterable) -> Generator[MsgData, None, None]:
    for toot in toots:
        if reply := notif2reply(toot):
            yield reply


def notif2reply(toots: list[AttribAccessDict]) -> Optional[MsgData]:
    toot_type = toots[0].type
    name = ", ".join(_get_name(t.account) for t in toots)

    if toot_type == "follow":
        return MsgData(text=f"👤 {name} followed you.")

    if toot_type == "reblog":
        text = f"🔁 {name} boosted your toot."
    elif toot_type == "favourite":
        text = f"⭐ {name} favorited your toot."
    else:  # unsupported type
        assert toot_type != "mention"  # mentions are handled with toots2replies
        return None

    toot = toots[0].status
    text += f"\n\n[{v2emoji[toot.visibility]} {toot.created_at.strftime(STRFORMAT)}]({toot.url})"
    return MsgData(text=text, html=toot.content)


def get_extension(resp: requests.Response) -> str:
    disp = resp.headers.get("content-disposition")
    if disp is not None and re.findall("filename=(.+)", disp):
        fname = re.findall("filename=(.+)", disp)[0].strip('"')
    else:
        fname = resp.url.split("/")[-1].split("?")[0].split("#")[0]
    if "." in fname:
        ext = "." + fname.rsplit(".", maxsplit=1)[-1]
    else:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = mimetypes.guess_extension(ctype) or ""
    return ext


def get_user(m, user_id) -> Any:
    user = None
    if user_id.isdigit():
        user = m.account(user_id)
    else:
        user_id = user_id.lstrip("@").lower()
        ids = (user_id, user_id.split("@")[0])
        for a in m.account_search(user_id):
            if a.acct.lower() in ids:
                user = a
                break
    return user


@contextmanager
def download_file(url: str, default_extension="") -> Generator[str, None, None]:
    """Download a blob and save it in temporary file."""
    with web.get(url) as resp:
        ext = get_extension(resp) or default_extension
        content = resp.content
    with NamedTemporaryFile(suffix=ext) as temp_file:
        temp_file.write(content)
        temp_file.flush()
        try:
            yield temp_file.name
        except GeneratorExit:
            pass


def normalize_url(url: str) -> str:
    if url.startswith("http://"):
        url = "https://" + url[4:]
    elif not url.startswith("https://"):
        url = "https://" + url
    return url.rstrip("/")


def get_profile(masto: Mastodon, username: Optional[str] = None) -> str:
    me = masto.me()
    if not username:
        user = me
    else:
        user = get_user(masto, username)
        if user is None:
            return "❌ Invalid user"

    text = f"{_get_name(user)}:\n\n"
    fields = ""
    for f in user.fields:
        fields += f"{html2text(f.name).strip()}: {html2text(f.value).strip()}\n"
    if fields:
        text += fields + "\n\n"
    text += html2text(user.note).strip()
    text += (
        f"\n\nToots: {user.statuses_count}\n"
        f"Following: {user.following_count}\n"
        f"Followers: {user.followers_count}"
    )
    if user.id != me.id:
        rel = masto.account_relationships(user)[0]
        if rel["followed_by"]:
            text += "\n[follows you]"
        elif rel["blocked_by"]:
            text += "\n[blocked you]"
        text += "\n"
        if rel["following"] or rel["requested"]:
            action = "unfollow"
        else:
            action = "follow"
        text += f"\n/{action}_{user.id}"
        action = "unmute" if rel["muting"] else "mute"
        text += f"\n/{action}_{user.id}"
        action = "unblock" if rel["blocking"] else "block"
        text += f"\n/{action}_{user.id}"
        text += f"\n/dm_{user.id}"
    text += TOOT_SEP
    toots = masto.account_statuses(user, limit=10)
    text += TOOT_SEP.join(toots2texts(reversed(toots)))
    return text


def listen_to_mastodon(bot: Bot, args: Namespace) -> None:
    while True:
        try:
            _check_mastodon(bot, args)
        except Exception as ex:
            bot.logger.exception(ex)


def _check_mastodon(bot: Bot, args: Namespace) -> None:
    accid = bot.rpc.get_all_account_ids()[0]
    while True:
        bot.logger.debug("Checking Mastodon")
        instances: dict = {}
        with session_scope() as session:
            acc_count = session.query(Account).count()
            bot.logger.debug(f"Accounts to check: {acc_count}")
            for acc in session.query(Account):
                instances.setdefault(acc.url, []).append(
                    (
                        acc.addr,
                        acc.token,
                        acc.home,
                        acc.last_home,
                        acc.muted_home,
                        acc.notifications,
                        acc.last_notif,
                        acc.muted_notif,
                    )
                )

        start_time = time.time()
        instances_count = len(instances)
        total_acc = acc_count
        while instances:
            bot.logger.debug(
                f"Check: {acc_count} accounts across {instances_count} instances remaining..."
            )
            for key in list(instances.keys()):
                (
                    addr,
                    token,
                    home_chat,
                    last_home,
                    muted_home,
                    notif_chat,
                    last_notif,
                    muted_notif,
                ) = instances[key].pop()
                acc_count -= 1
                if not instances[key]:
                    instances.pop(key)
                    instances_count -= 1
                bot.logger.debug(f"{addr}: Checking account ({key})")
                try:
                    masto = get_mastodon(key, token)
                    _check_notifications(
                        bot, accid, masto, addr, notif_chat, last_notif, muted_notif
                    )
                    if muted_home:
                        bot.logger.debug(f"{addr}: Ignoring Home timeline (muted)")
                    else:
                        _check_home(bot, accid, masto, addr, home_chat, last_home)
                    bot.logger.debug(f"{addr}: Done checking account")
                except MastodonUnauthorizedError as ex:
                    bot.logger.exception(ex)
                    chats: List[int] = []
                    with session_scope() as session:
                        acc = session.query(Account).filter_by(addr=addr).first()
                        if acc:
                            chats.extend(dmchat.chat_id for dmchat in acc.dm_chats)
                            chats.append(acc.home)
                            chats.append(acc.notifications)
                            session.delete(acc)
                    for chat_id in chats:
                        try:
                            bot.rpc.leave_group(accid, chat_id)
                        except JsonRpcError:
                            pass

                    contactid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
                    chatid = bot.rpc.create_chat_by_contact_id(accid, contactid)
                    text = f"❌ ERROR Your account was logged out: {ex}"
                    bot.rpc.send_msg(accid, chatid, MsgData(text=text))
                except (MastodonNetworkError, MastodonServerError) as ex:
                    bot.logger.exception(ex)
                except Exception as ex:  # noqa
                    bot.logger.exception(ex)
                    contactid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
                    chatid = bot.rpc.create_chat_by_contact_id(accid, contactid)
                    text = f"❌ ERROR while checking your account: {ex}"
                    bot.rpc.send_msg(accid, chatid, MsgData(text=text))
            time.sleep(2)
        elapsed = int(time.time() - start_time)
        delay = max(args.interval - elapsed, 10)
        bot.logger.info(f"Done checking {total_acc} accounts, sleeping for {delay} seconds...")
        time.sleep(delay)


def send_toot(
    masto: Mastodon,
    text: Optional[str] = None,
    filename: Optional[str] = None,
    visibility: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> None:
    if filename:
        if filename.endswith(".aac"):
            aac_file = AudioSegment.from_file(filename, "aac")
            filename = filename[:-4] + ".mp3"
            aac_file.export(filename, format="mp3")
        media = [masto.media_post(filename).id]
        if in_reply_to:
            masto.status_reply(
                masto.status(in_reply_to), text, media_ids=media, visibility=visibility
            )
        else:
            masto.status_post(text, media_ids=media, visibility=visibility)
    elif text:
        if in_reply_to:
            masto.status_reply(masto.status(in_reply_to), text, visibility=visibility)
        else:
            masto.status_post(text, visibility=visibility)


def get_client(session, api_url) -> tuple:
    client = session.query(Client).filter_by(url=api_url).first()
    if client:
        return client.id, client.secret

    try:
        client_id, client_secret = Mastodon.create_app(
            client_name="DeltaChat Bridge",
            website="https://github.com/simplebot-org/simplebot_mastodon",
            redirect_uris="urn:ietf:wg:oauth:2.0:oob",
            api_base_url=api_url,
            session=web,
        )
    except Exception:  # noqa
        client_id, client_secret = None, None
    session.add(Client(url=api_url, id=client_id, secret=client_secret))
    return client_id, client_secret


def get_mastodon(api_url: str, token: Optional[str] = None, **kwargs) -> Mastodon:
    return Mastodon(
        access_token=token,
        api_base_url=api_url,
        ratelimit_method="throw",
        session=web,
        **kwargs,
    )


def get_mastodon_from_msg(bot, accid, msg) -> Optional[Mastodon]:
    api_url, token = "", ""
    chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
    with session_scope() as session:
        acc = get_account_from_msg(chat, msg, session)
        if acc:
            api_url, token = acc.url, acc.token
    return get_mastodon(api_url, token) if api_url else None


def get_account_from_msg(chat, msg, session) -> Optional[Account]:
    acc = get_account_from_chat(chat, session)
    if not acc:
        addr = msg.sender.address
        acc = session.query(Account).filter_by(addr=addr).first()
    return acc


def get_account_from_chat(chat, session) -> Optional[Account]:
    if chat.chat_type == ChatType.SINGLE:
        return None

    acc = (
        session.query(Account)
        .filter((Account.home == chat.id) | (Account.notifications == chat.id))
        .first()
    )
    if not acc:
        dmchat = session.query(DmChat).filter_by(chat_id=chat.id).first()
        if dmchat:
            acc = dmchat.account
    return acc


def account_action(action: str, payload: str, bot, accid, msg) -> str:
    if not payload:
        return "❌ Wrong usage"

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        if payload.isdigit():
            user_id = payload
        else:
            user_id = get_user(masto, payload)
            if user_id is None:
                return "❌ Invalid user"
        getattr(masto, action)(user_id)
        return ""
    return "❌ You are not logged in"


def _get_name(macc) -> str:
    isbot = "[BOT] " if macc.bot else ""
    if macc.display_name:
        return isbot + f"{macc.display_name} (@{macc.acct})"
    return isbot + macc.acct


def _handle_dms(bot: Bot, accid: int, dms: list, addr: str, notif_chat: int) -> None:
    def _get_chat_id(acct) -> int:
        with session_scope() as session:
            dmchat = session.query(DmChat).filter_by(acc_addr=addr, contact=acct).first()
            if dmchat:
                chat_id = dmchat.chat_id
            else:
                chat_id = 0
        return chat_id

    def send_reply(dm, reply: MsgData) -> None:
        acct = dm.account.acct
        chat_id = chats.get(acct, 0)
        if not chat_id:
            chat_id = chats[acct] = _get_chat_id(acct)

        if not chat_id:
            chat_id = bot.rpc.create_group_chat(accid, acct, False)
            chats[acct] = chat_id
            for conid in bot.rpc.get_chat_contacts(accid, notif_chat):
                if conid != SpecialContactId.SELF:
                    bot.rpc.add_contact_to_chat(accid, chat_id, conid)

            with session_scope() as session:
                session.add(DmChat(chat_id=chat_id, contact=acct, acc_addr=addr))

            try:
                url = dm.account.avatar_static
                with download_file(url, ".jpg") as path:
                    bot.rpc.set_chat_profile_image(accid, chat_id, path)
            except Exception as err:
                bot.logger.exception(err)

        bot.rpc.send_msg(accid, chat_id, reply)

    chats: Dict[str, int] = {}
    for dm in reversed(dms):
        reply = toot2reply(dm)
        if reply.file:
            try:
                with download_file(bot, reply.file) as path:
                    reply.file = path
                    send_reply(dm, reply)
                    continue
            except Exception as ex:
                bot.logger.exception(ex)
                text = reply.text or ""
                if not text.startswith("http"):
                    text = "\n" + text
                reply.text = reply.file + "\n" + text
                reply.file = None
        send_reply(dm, reply)


def _check_notifications(
    bot: Bot,
    accid: int,
    masto: Mastodon,
    addr: str,
    notif_chat: int,
    last_id: str,
    muted_notif: bool,
) -> None:
    dms = []
    notifications = []
    bot.logger.debug(f"{addr}: Getting Notifications (last_id={last_id})")
    toots = masto.notifications(min_id=last_id, limit=100)
    if toots:
        with session_scope() as session:
            acc = session.query(Account).filter_by(addr=addr).first()
            acc.last_notif = last_id = toots[0].id
        for toot in toots:
            if (
                toot.type == "mention"
                and toot.status.visibility == Visibility.DIRECT
                and len(toot.status.mentions) == 1
            ):
                content = toot.status.content
                if not any(keyword in content for keyword in SPAM):
                    dms.append(toot.status)
            elif not muted_notif or toot.type not in MUTED_NOTIFICATIONS:
                notifications.append(toot)

    if dms:
        bot.logger.debug(f"{addr}: Direct Messages: {len(dms)} new entries")
        _handle_dms(bot, accid, dms, addr, notif_chat)

    bot.logger.debug(f"{addr}: Notifications: {len(notifications)} new entries (last_id={last_id})")
    if notifications:
        reblogs: dict[str, AttribAccessDict] = {}
        favs: dict[str, AttribAccessDict] = {}
        follows = []
        mentions = []
        for toot in reversed(notifications):
            if toot.type == "reblog":
                reblogs.setdefault(toot.status.id, []).append(toot)
            elif toot.type == "favourite":
                favs.setdefault(toot.status.id, []).append(toot)
            elif toot.type == "follow":
                follows.append(toot)
            else:
                mentions.append(toot.status)
        notifs = [*reblogs.values(), *favs.values()]
        if follows:
            notifs.append(follows)

        for reply in notif2replies(notifs):
            bot.rpc.send_msg(accid, notif_chat, reply)
        for reply in toots2replies(bot, mentions):
            bot.rpc.send_msg(accid, notif_chat, reply)


def _check_home(
    bot: Bot, accid: int, masto: Mastodon, addr: str, home_chat: int, last_id: str
) -> None:
    me = masto.me()
    bot.logger.debug(f"{addr}: Getting Home timeline (last_id={last_id})")
    toots = masto.timeline_home(min_id=last_id, limit=100)
    if toots:
        with session_scope() as session:
            acc = session.query(Account).filter_by(addr=addr).first()
            acc.last_home = last_id = toots[0].id
        toots = [toot for toot in toots if me.id not in [acc.id for acc in toot.mentions]]
    bot.logger.debug(f"{addr}: Home: {len(toots)} new entries (last_id={last_id})")
    if toots:
        for reply in toots2replies(bot, reversed(toots)):
            bot.rpc.send_msg(accid, home_chat, reply)
