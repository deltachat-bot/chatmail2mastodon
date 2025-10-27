# Mastodon Bridge

[![Latest Release](https://img.shields.io/pypi/v/pixelsocial.svg)](https://pypi.org/project/chatmail2mastodon)
[![CI](https://github.com/deltachat-bot/chatmail2mastodon/actions/workflows/python-ci.yml/badge.svg)](https://github.com/deltachat-bot/chatmail2mastodon/actions/workflows/python-ci.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A Mastodon <-> Chatmail bridge.

## Install

To install run:

```
pip install chatmail2mastodon
```

## Configuration

To configure the bot:

```sh
chatmail2mastodon init DCACCOUNT:https://nine.testrun.org/new
```

**(Optional)** To customize the bot name, avatar and status/signature:

```sh
chatmail2mastodon config selfavatar "/path/to/avatar.png"
chatmail2mastodon config displayname "My Bot"
chatmail2mastodon config selfstatus "Hi, I am a bot!"
```

Finally you can start the bot with:

```sh
chatmail2mastodon serve
```

To see the available options, run in the command line:

```
chatmail2mastodon --help
```

## User Guide

To log in with OAuth, send a message to the bot:

```
/login mastodon.social
```

replace "mastodon.social" with your instance, the bot will reply with an URL that you should open to grant access to your account, copy the code you will receive and send it to the bot.

To log in with your user and password directly(not recommended):

```
/login mastodon.social me@example.com myPassw0rd
```

Once you log in, A "Home" and "Notifications" chats will appear, in the Home chat you will receive your Home timeline and any message you send there will be published on Mastodon. In the Notifications chat you will receive all the notifications for your account.

If someone sends you a direct message in a private 1:1 conversation, it will be shown as a new chat where you can chat in private with that person, to start a private chat with some Mastodon user, send:

```
/dm friend@example.com
```

and the chat with "friend@example.com" will pop up.

To logout from your account:

```
/logout
```

For more info and all the available commands(follow, block, mute, etc), send this message to the bot:

```
/help
```