"""Event handlers and hooks"""

import os
from argparse import Namespace
from pathlib import Path
from threading import Thread

import mastodon
from deltachat2 import (
    Bot,
    ChatType,
    CoreEvent,
    EventType,
    JsonRpcError,
    MsgData,
    NewMsgEvent,
    SpecialContactId,
    SystemMessageType,
    events,
)
from rich.logging import RichHandler

from .cli import cli
from .migrations import run_migrations
from .orm import Account, DmChat, OAuth, initdb, session_scope
from .util import (
    TOOT_SEP,
    Visibility,
    account_action,
    download_file,
    get_account_from_msg,
    get_client,
    get_mastodon,
    get_mastodon_from_msg,
    get_profile,
    get_user,
    listen_to_mastodon,
    normalize_url,
    send_toot,
    toots2texts,
)

MASTODON_LOGO = os.path.join(os.path.dirname(__file__), "mastodon-logo.png")


@cli.on_init
def on_init(bot: Bot, args: Namespace) -> None:
    bot.logger.handlers = [
        RichHandler(show_path=False, omit_repeated_times=False, show_time=args.no_time)
    ]
    self_status = (
        "I am a bot that allows you to use your Mastodon"
        " social network account.\n\nSource code: "
        "https://github.com/deltachat-bot/chatmail2mastodon"
    )
    for accid in bot.rpc.get_all_account_ids():
        if not bot.rpc.get_config(accid, "displayname"):
            bot.rpc.set_config(accid, "displayname", "Mastodon Bridge")
            bot.rpc.set_config(accid, "selfstatus", self_status)


@cli.on_start
def on_start(bot: Bot, args: Namespace) -> None:
    dbpath = Path(args.config_dir, "sqlite.db")
    run_migrations(bot, dbpath)
    initdb(f"sqlite:///{dbpath}")
    Thread(target=listen_to_mastodon, args=(bot, args), daemon=True).start()


@cli.on(events.RawEvent)
def log_event(bot: Bot, accid: int, event: CoreEvent) -> None:
    if event.kind == EventType.INFO:
        bot.logger.debug(event.msg)
    elif event.kind == EventType.WARNING:
        bot.logger.warning(event.msg)
    elif event.kind == EventType.ERROR:
        bot.logger.error(event.msg)
    elif event.kind == EventType.SECUREJOIN_INVITER_PROGRESS:
        if event.progress == 1000:
            if not bot.rpc.get_contact(accid, event.contact_id).is_bot:
                bot.logger.debug("QR scanned by contact id=%s", event.contact_id)
                chatid = bot.rpc.create_chat_by_contact_id(accid, event.contact_id)
                send_help(bot, accid, chatid)


@cli.on(events.NewMessage(is_info=True))
def on_info_msg(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    """Process member-removed messages"""
    msg = event.msg
    chatid = msg.chat_id

    if (
        msg.system_message_type != SystemMessageType.MEMBER_REMOVED_FROM_GROUP
        or not msg.info_contact_id
    ):
        return

    contactid = msg.info_contact_id
    if contactid != SpecialContactId.SELF and len(bot.rpc.get_chat_contacts(accid, chatid)) > 1:
        return

    url = ""
    addr = ""
    chats: list[int] = []
    with session_scope() as session:
        acc = (
            session.query(Account)
            .filter((Account.home == chatid) | (Account.notifications == chatid))
            .first()
        )
        if acc:
            url = acc.url
            addr = acc.addr
            chats.extend(dmchat.chat_id for dmchat in acc.dm_chats)
            chats.append(acc.home)
            chats.append(acc.notifications)
            session.delete(acc)
        else:
            dmchat = session.query(DmChat).filter_by(chat_id=chatid).first()
            if dmchat:
                chats.append(chatid)
                session.delete(dmchat)

    for chatid in chats:
        try:
            bot.rpc.leave_group(accid, chatid)
        except JsonRpcError:
            pass

    if url:
        contactid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
        chatid = bot.rpc.create_chat_by_contact_id(accid, contactid)
        text = f"✔️ You logged out from: {url}"
        bot.rpc.send_msg(accid, chatid, MsgData(text=text))


@cli.on(events.NewMessage(is_info=False))
def on_msg(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    """Process messages in Mastodon-bridge related chats"""
    if bot.has_command(event.command):
        return

    msg = event.msg
    chatid = msg.chat_id
    chat = bot.rpc.get_basic_chat_info(accid, chatid)

    if chat.chat_type == ChatType.SINGLE:
        bot.rpc.markseen_msgs(accid, [msg.id])
        addr = msg.sender.address
        with session_scope() as session:
            auth = session.query(OAuth).filter_by(addr=addr).first()
            if not auth:
                text = "❌ To publish messages you must send them in your Home chat."
                reply = MsgData(text=text, quoted_message_id=msg.id)
                bot.rpc.send_msg(accid, chatid, reply)
                return
            url, user, client_id, client_secret = (
                auth.url,
                auth.user,
                auth.client_id,
                auth.client_secret,
            )
        m = get_mastodon(url, client_id=client_id, client_secret=client_secret)
        try:
            m.log_in(code=msg.text.strip())
            _login(bot, accid, chatid, addr, user, m)
            with session_scope() as session:
                session.delete(session.query(OAuth).filter_by(addr=addr).first())
        except Exception as err:  # noqa
            bot.logger.exception(err)
            text = "❌ Authentication failed, generate another authorization code and send it here"
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
        return

    api_url: str = ""
    token = ""
    args: tuple = ()
    with session_scope() as session:
        acc = (
            session.query(Account)
            .filter((Account.home == chatid) | (Account.notifications == chatid))
            .first()
        )
        if acc:
            if acc.home == chatid:
                api_url = acc.url
                token = acc.token
                args = (msg.text, msg.file)
        elif len(bot.rpc.get_chat_contacts(accid, chatid)) <= 2:
            # only send directly if not in team usage
            dmchat = session.query(DmChat).filter_by(chat_id=chatid).first()
            if dmchat:
                api_url = dmchat.account.url
                token = dmchat.account.token
                args = (
                    f"@{dmchat.contact} {msg.text}",
                    msg.file,
                    Visibility.DIRECT,
                )

    if api_url:
        send_toot(get_mastodon(api_url, token), *args)


@cli.on(events.NewMessage(command="/help"))
def _help_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    send_help(bot, accid, event.msg.chat_id)


@cli.on(events.NewMessage(command="/login"))
def _login_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    args = event.payload.split(maxsplit=2)
    if len(args) == 1:
        api_url, email, passwd = args[0], None, None
    else:
        if len(args) != 3:
            reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
            return
        api_url, email, passwd = args
    api_url = normalize_url(api_url)
    addr = msg.sender.address

    user = ""
    with session_scope() as session:
        acc = session.query(Account).filter_by(addr=addr).first()
        if acc:
            if acc.url != api_url:
                text = "❌ You are already logged in."
                bot.rpc.send_msg(accid, chatid, MsgData(text=text))
                return
            user = acc.user

        client_id, client_secret = get_client(session, api_url)

    m = get_mastodon(api_url, client_id=client_id, client_secret=client_secret)

    if email:
        m.log_in(email, passwd)
        _login(bot, accid, chatid, addr, user, m)
    else:
        if client_id is None:
            text = "❌ Server doesn't seem to support OAuth."
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
            return
        with session_scope() as session:
            auth = session.query(OAuth).filter_by(addr=addr).first()
            if not auth:
                session.add(
                    OAuth(
                        addr=addr,
                        url=api_url,
                        user=user,
                        client_id=client_id,
                        client_secret=client_secret,
                    )
                )
            else:
                auth.url = api_url
                auth.client_id = client_id
                auth.client_secret = client_secret
                auth.user = user
        auth_url = m.auth_request_url()
        text = (
            f"To grant access to your account, open this URL:\n\n{auth_url}\n\n"
            "You will get an authorization code, copy it and send it here"
        )
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


def _login(
    bot: Bot, accid: int, chatid: int, addr: str, user: str, masto: mastodon.Mastodon
) -> None:
    uname = masto.me().acct.lower()

    if user:
        if user == uname:
            with session_scope() as session:
                acc = session.query(Account).filter_by(addr=addr).first()
                acc.token = masto.access_token
                text = "✔️ You refreshed your credentials."
                bot.rpc.send_msg(accid, chatid, MsgData(text=text))
        else:
            text = "❌ You are already logged in."
            bot.rpc.send_msg(accid, chatid, MsgData(text=text))
        return

    n = masto.notifications(limit=1)
    last_notif = n[0].id if n else None
    n = masto.timeline_home(limit=1)
    last_home = n[0].id if n else None

    api_url = masto.api_base_url
    url = api_url.split("://", maxsplit=1)[-1]
    hgroup = bot.rpc.create_group_chat(accid, f"Home ({url})", False)
    ngroup = bot.rpc.create_group_chat(accid, f"Notifications ({url})", False)
    contactid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
    bot.rpc.add_contact_to_chat(accid, hgroup, contactid)
    bot.rpc.add_contact_to_chat(accid, ngroup, contactid)

    with session_scope() as session:
        session.add(
            Account(
                addr=addr,
                user=uname,
                url=api_url,
                token=masto.access_token,
                home=hgroup,
                notifications=ngroup,
                last_home=last_home,
                last_notif=last_notif,
            )
        )

    bot.rpc.set_chat_profile_image(accid, hgroup, MASTODON_LOGO)
    text = (
        "ℹ️ Messages sent here will be published in"
        f" @{uname}@{url}\n\n"
        "If your Home timeline is too noisy and you would like"
        " to disable incoming toots, send /mute here."
    )
    bot.rpc.send_msg(accid, hgroup, MsgData(text=text))

    bot.rpc.set_chat_profile_image(accid, ngroup, MASTODON_LOGO)
    text = f"ℹ️ Here you will receive notifications for @{uname}@{url}"
    bot.rpc.send_msg(accid, ngroup, MsgData(text=text))


@cli.on(events.NewMessage(command="/logout"))
def _logout_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    addr = msg.sender.address
    chats: list[int] = []
    with session_scope() as session:
        acc = session.query(Account).filter_by(addr=addr).first()
        if acc:
            text = f"✔️ You logged out from: {acc.url}"
            chats.extend(dmchat.chat_id for dmchat in acc.dm_chats)
            chats.append(acc.home)
            chats.append(acc.notifications)
            session.delete(acc)
        else:
            text = "❌ You are not logged in"

    for chatid in chats:
        try:
            bot.rpc.leave_group(accid, chatid)
        except JsonRpcError:
            pass
    contactid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
    chatid = bot.rpc.create_chat_by_contact_id(accid, contactid)
    bot.rpc.send_msg(accid, chatid, MsgData(text=text))


@cli.on(events.NewMessage(command="/bio"))
def _bio_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id

    if not event.payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        try:
            masto.account_update_credentials(note=event.payload)
            text = "✔️ Biography updated"
        except mastodon.MastodonAPIError as err:
            text = f"❌ ERROR: {err.args[-1]}"
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/avatar"))
def _avatar_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id

    if not msg.file:
        text = "❌ You must send an avatar attached to your message"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        try:
            masto.account_update_credentials(avatar=msg.file)
            text = "✔️ Avatar updated"
        except mastodon.MastodonAPIError:
            text = "❌ Failed to update avatar"
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/dm"))
def _dm_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id

    if not event.payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        username = event.payload.lstrip("@").lower()
        user = get_user(masto, username)
        if not user:
            text = f"❌ Account not found: {username}"
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
            return

        chat = bot.rpc.get_basic_chat_info(accid, chatid)
        with session_scope() as session:
            acc = get_account_from_msg(chat, msg, session)
            assert acc
            dmchat = session.query(DmChat).filter_by(acc_addr=acc.addr, contact=user.acct).first()
            if dmchat:
                text = "❌ Chat already exists, send messages here"
                bot.rpc.send_msg(accid, dmchat.chat_id, MsgData(text=text))
                return
            chatid = bot.rpc.create_group_chat(accid, user.acct, False)
            for conid in bot.rpc.get_chat_contacts(accid, acc.notifications):
                if conid != SpecialContactId.SELF:
                    bot.rpc.add_contact_to_chat(accid, chatid, conid)
            session.add(DmChat(chat_id=chatid, contact=user.acct, acc_addr=acc.addr))

        try:
            with download_file(user.avatar_static, ".jpg") as path:
                bot.rpc.set_chat_profile_image(accid, chatid, path)
        except Exception as err:
            bot.logger.exception(err)
        text = f"ℹ️ Private chat with: {user.acct}"
        bot.rpc.send_msg(accid, chatid, MsgData(text=text))
    else:
        text = "❌ You are not logged in"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/reply"))
def _reply_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    args = event.payload.split(maxsplit=1)
    if len(args) != 2 and not (args and msg.file):
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    toot_id = args.pop(0)
    text = args.pop(0) if args else ""

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        send_toot(masto, text=text, filename=msg.file, in_reply_to=toot_id)
    else:
        text = "❌ You are not logged in"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/star"))
def _star_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id

    if not event.payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        masto.status_favourite(event.payload)
    else:
        text = "❌ You are not logged in"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/boost"))
def _boost_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id

    if not event.payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        masto.status_reblog(event.payload)
    else:
        text = "❌ You are not logged in"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/open"))
def _open_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    payload = event.payload
    msg = event.msg
    chatid = msg.chat_id

    if not payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        context = masto.status_context(payload)
        toots = context["ancestors"] + [masto.status(payload)] + context["descendants"]
        text = TOOT_SEP.join(toots2texts(toots)) if toots else "❌ Nothing found"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
    else:
        text = "❌ You are not logged in"
        reply = MsgData(text=text, quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/follow"))
def _follow_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    text = account_action("account_follow", event.payload, bot, accid, msg)
    reply = MsgData(text=text or "✔️ User followed", quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/unfollow"))
def _unfollow_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    text = account_action("account_unfollow", event.payload, bot, accid, msg)
    reply = MsgData(text=text or "✔️ User unfollowed", quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/mute"))
def _mute_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    if event.payload:
        text = account_action("account_mute", event.payload, bot, accid, msg)
        reply = MsgData(text=text or "✔️ User muted", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    # check if the message was sent in the Home or Notifications chat
    with session_scope() as session:
        acc = session.query(Account).filter_by(home=msg.chat_id).first()
        if acc:
            acc.muted_home = True
            acc.last_home = None
            text = "✔️ Home timeline muted"
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
            return

        acc = session.query(Account).filter_by(notifications=msg.chat_id).first()
        if acc:
            acc.muted_notif = True
            text = (
                "✔️ Notifications timeline muted: follows,"
                " favorites and boosts will not be notified"
            )
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
        else:
            text = (
                "❌ Wrong usage, you must send that command"
                " in the Home or Notifications chat to mute them"
            )
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/unmute"))
def _unmute_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    if event.payload:
        text = account_action("account_unmute", event.payload, bot, accid, msg)
        reply = MsgData(text=text or "✔️ User unmuted", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    # check if the message was sent in the Home or Notifications chat
    with session_scope() as session:
        acc = session.query(Account).filter_by(home=msg.chat_id).first()
        if acc:
            acc.muted_home = False
            acc.last_home = None
            text = "✔️ Home timeline unmuted"
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
            return

        acc = session.query(Account).filter_by(notifications=msg.chat_id).first()
        if acc:
            acc.muted_notif = False
            text = "✔️ Notifications timeline unmuted"
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)
        else:
            text = (
                "❌ Wrong usage, you must send that command in"
                " the Home or Notifications chat to unmute them"
            )
            reply = MsgData(text=text, quoted_message_id=msg.id)
            bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/block"))
def _block_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    text = account_action("account_block", event.payload, bot, accid, msg)
    reply = MsgData(text=text or "✔️ User blocked", quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/unblock"))
def _unblock_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    text = account_action("account_unblock", event.payload, bot, accid, msg)
    reply = MsgData(text=text or "✔️ User unblocked", quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/profile"))
def _profile_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        text = get_profile(masto, event.payload)
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/local"))
def _local_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        text = TOOT_SEP.join(toots2texts(reversed(masto.timeline_local()))) or "❌ Nothing found"
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/public"))
def _public_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        text = TOOT_SEP.join(toots2texts(reversed(masto.timeline_public()))) or "❌ Nothing found"
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, msg.chat_id, reply)


@cli.on(events.NewMessage(command="/tag"))
def _tag_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    payload = event.payload

    if not payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    tag = payload.lstrip("#")
    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        text = (
            TOOT_SEP.join(toots2texts(reversed(masto.timeline_hashtag(tag)))) or "❌ Nothing found"
        )
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, chatid, reply)


@cli.on(events.NewMessage(command="/search"))
def _search_cmd(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    msg = event.msg
    chatid = msg.chat_id
    payload = event.payload

    if not payload:
        reply = MsgData(text="❌ Wrong usage", quoted_message_id=msg.id)
        bot.rpc.send_msg(accid, chatid, reply)
        return

    masto = get_mastodon_from_msg(bot, accid, msg)
    if masto:
        res = masto.search(payload)
        text = ""
        if res["accounts"]:
            text += "👤 Accounts:"
            for a in res["accounts"]:
                text += f"\n@{a.acct} /profile_{a.id}"
            text += "\n\n"
        if res["hashtags"]:
            text += "#️⃣ Hashtags:"
            for tag in res["hashtags"]:
                text += f"\n#{tag.name} /tag_{tag.name}"
        if not text:
            text = "❌ Nothing found"
    else:
        text = "❌ You are not logged in"
    reply = MsgData(text=text, quoted_message_id=msg.id)
    bot.rpc.send_msg(accid, chatid, reply)


def send_help(bot: Bot, accid: int, chatid: int) -> None:
    text = """
Hi, I am a Mastodon bridge bot.

Use /login to log in, once you log in with your Mastodon credentials, two chats will be created for you:

    • The Home chat is where you will receive your Home timeline and any message you send in that chat will be published on Mastodon.
    • The Notifications chat is where you will receive your Mastodon notifications.

    When a Mastodon user writes a private/direct message to you, a chat will be created for your private conversation with that user.

**Available commands**

/login - Login on Mastodon. Example:
    /login mastodon.social
    To login without OAuth:
    /login mastodon.social me@example.com myPassw0rd

/logout - Logout from Mastodon.

/bio - Update your Mastodon biography. Example:
    /bio I love Delta Chat

/avatar - Update your Mastodon avatar. Together with this command, you must attach the avatar image you want to set.

/dm - Start a private chat with the given Mastodon user. Example:
    /dm user@mastodon.social

/reply - Reply to a toot with the given id.

/star - Mark as favourite the toot with the given id.

/boost - Boost the toot with the given id.

/open - Open the thread of the toot with the given id.

/follow - Follow the user with the given account name or id. Example:
    /follow user@mastodon.social

/unfollow - Unfollow the user with the given account name or id. Example:
    /unfollow user@mastodon.social

/mute - Mute the user with the given account name or id. If sent in the Home chat it will mute the Home timeline. Example:
    /mute user@mastodon.social
    To mute Home timeline:
    /mute

/unmute - Unmute the user with the given account name or id. If sent in the Home chat it will unmute the Home timeline. Example:
    /unmute user@mastodon.social
    To unmute Home timeline:
    /unmute

/block - Block the user with the given account name or id. Example:
    /block user@mastodon.social

/unblock - Unblock the user with the given account name or id. Example:
    /unblock user@mastodon.social

/profile - See the profile of the given user. Example:
    /profile user@mastodon.social

/local - Get latest entries from the local timeline.

/public - Get latest entries from the public timeline.

/tag - Get latest entries with the given hashtags. Example:
    /tag mastocat

/search - Search for users and hashtags matching the given text. Example:
    /search deltachat
    """
    bot.rpc.send_msg(accid, chatid, MsgData(text=text))
